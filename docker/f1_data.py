"""Shared F1 data client backed by OpenF1 and Jolpica-F1 (no auth required)."""

from collections import defaultdict, deque
import datetime
import hashlib
import json
import logging
import os
import re
import time

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

OPENF1_BASE_URL = "https://api.openf1.org/v1"
JOLPICA_BASE_URL = "https://api.jolpi.ca/ergast/f1"
USER_AGENT = "F1-Watchability/2.0 (https://github.com/ankurmathur/F1-Watchability)"

# Finished status pattern matching original calibration formula (plus "Lapped" in newer Jolpica data)
FINISHED_STATUS_RE = re.compile(r"Finished|\+\d+\s*Laps?|Lapped", re.IGNORECASE)
LEGACY_FINISHED_STATUS_RE = re.compile(r"Finished|\+1 Lap|\+2 Laps|\+3 Laps", re.IGNORECASE)


class F1DataClient:
    """Rate-limited, disk-cached client for OpenF1 and Jolpica-F1 APIs."""

    def __init__(self, cache_dir=None):
        self.cache_dir = cache_dir
        if self.cache_dir:
            self.api_cache_dir = os.path.join(self.cache_dir, "api_cache")
            os.makedirs(self.api_cache_dir, exist_ok=True)
        else:
            self.api_cache_dir = None

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

        # Rate-limit trackers (timestamps of recent requests)
        self._openf1_timestamps = deque()
        self._jolpica_last_req = 0.0

        # In-memory cache for schedules and sessions within a run
        self._schedule_mem_cache = {}
        self._openf1_sessions_mem_cache = {}
        self._results_mem_cache = {}

    def _throttle_openf1(self):
        """Respect OpenF1 free-tier limits (3 req/s, 30 req/min)."""
        now = time.monotonic()
        # Keep timestamps within the last 60 seconds
        while self._openf1_timestamps and (now - self._openf1_timestamps[0]) > 60.5:
            self._openf1_timestamps.popleft()

        # Stay comfortably below 30 req/min (cap at 27/min)
        if len(self._openf1_timestamps) >= 27:
            sleep_for = 60.5 - (now - self._openf1_timestamps[0])
            if sleep_for > 0:
                logger.debug(f"OpenF1 minute rate-limit throttle: sleeping {sleep_for:.2f}s")
                time.sleep(sleep_for)
            now = time.monotonic()
            while self._openf1_timestamps and (now - self._openf1_timestamps[0]) > 60.5:
                self._openf1_timestamps.popleft()

        # Stay below 3 req/s (min 0.40s spacing)
        if self._openf1_timestamps:
            elapsed = now - self._openf1_timestamps[-1]
            if elapsed < 0.40:
                time.sleep(0.40 - elapsed)

        self._openf1_timestamps.append(time.monotonic())

    def _throttle_jolpica(self):
        """Respect Jolpica rate limits (keep comfortable pacing to avoid 429s)."""
        now = time.monotonic()
        elapsed = now - self._jolpica_last_req
        if elapsed < 0.55:
            time.sleep(0.55 - elapsed)
        self._jolpica_last_req = time.monotonic()

    def _cache_path(self, namespace, key):
        if not self.api_cache_dir:
            return None
        safe_key = re.sub(r"[^a-zA-Z0-9_.-]", "_", key)
        if len(safe_key) > 120:
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
            safe_key = f"{safe_key[:96]}_{digest}"
        return os.path.join(self.api_cache_dir, f"{namespace}__{safe_key}.json")

    def _read_cache(self, path, ttl_seconds=None):
        if not path or not os.path.exists(path):
            return None
        try:
            if ttl_seconds is not None:
                age = time.time() - os.path.getmtime(path)
                if age > ttl_seconds:
                    return None
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _write_cache(self, path, data):
        if not path:
            return
        try:
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.debug(f"Failed to write cache {path}: {e}")

    def _get_json(self, url, params=None, provider="openf1", cache_key=None, ttl_seconds=None, cache_empty=False):
        cache_path = self._cache_path(provider, cache_key) if cache_key else None
        cached = self._read_cache(cache_path, ttl_seconds=ttl_seconds)
        if cached is not None:
            return cached

        for attempt in range(4):
            if provider == "openf1":
                self._throttle_openf1()
            else:
                self._throttle_jolpica()

            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    should_cache = cache_empty or self._has_data(data, provider)
                    if should_cache:
                        self._write_cache(cache_path, data)
                    return data
                if resp.status_code == 404:
                    return [] if provider == "openf1" else None
                if resp.status_code == 429:
                    wait = min(30, 2 ** (attempt + 1) + 1)
                    logger.warning(f"{provider} rate-limited (429) for {url}; retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"{provider} server error ({resp.status_code}) for {url}; retrying in {wait}s...")
                    time.sleep(wait)
                    continue

                logger.warning(f"{provider} HTTP {resp.status_code} for {url}")
                return None
            except requests.RequestException as e:
                wait = 2 ** (attempt + 1)
                logger.warning(f"{provider} request error for {url}: {e}; retrying in {wait}s...")
                time.sleep(wait)

        logger.error(f"{provider} failed after retries for {url}")
        return None

    @staticmethod
    def _has_data(data, provider):
        if data is None:
            return False
        if provider == "openf1":
            return isinstance(data, list) and len(data) > 0
        # Jolpica MRData structure
        if isinstance(data, dict) and "MRData" in data:
            total = data["MRData"].get("total")
            if total is not None:
                try:
                    return int(total) > 0
                except (ValueError, TypeError):
                    pass
            return True
        return bool(data)

    # ------------------------------------------------------------------
    # Jolpica-F1 methods
    # ------------------------------------------------------------------
    def get_schedule(self, year):
        """Return list of race events for `year` from Jolpica-F1."""
        year = int(year)
        if year in self._schedule_mem_cache:
            return self._schedule_mem_cache[year]

        current_year = datetime.datetime.utcnow().year
        ttl = None if year < current_year else 6 * 3600
        url = f"{JOLPICA_BASE_URL}/{year}.json"
        data = self._get_json(
            url,
            params={"limit": 100},
            provider="jolpica",
            cache_key=f"schedule_{year}",
            ttl_seconds=ttl,
        )
        if not data:
            return []

        races_raw = data.get("MRData", {}).get("RaceTable", {}).get("Races", [])
        events = []
        for race in races_raw:
            race_date_str = race.get("date")
            race_time_str = race.get("time", "14:00:00Z")
            race_dt = _parse_utc_datetime(race_date_str, race_time_str)

            sprint_obj = race.get("Sprint")
            has_sprint = isinstance(sprint_obj, dict) and bool(sprint_obj.get("date"))
            sprint_date_str = sprint_obj.get("date") if has_sprint else None
            sprint_dt = (
                _parse_utc_datetime(sprint_date_str, sprint_obj.get("time", "14:00:00Z"))
                if has_sprint
                else None
            )

            circuit = race.get("Circuit", {})
            location = circuit.get("Location", {})

            events.append({
                "year": year,
                "round": int(race["round"]),
                "gp_name": race["raceName"],
                "date": race_date_str,
                "race_datetime": race_dt,
                "has_sprint": has_sprint,
                "sprint_date": sprint_date_str,
                "sprint_datetime": sprint_dt,
                "country": location.get("country", ""),
                "locality": location.get("locality", ""),
            })

        events.sort(key=lambda e: e["round"])
        if events:
            self._schedule_mem_cache[year] = events
        return events

    def get_session_results(self, year, round_num, session_type="R"):
        """
        Return official driver classification rows from Jolpica-F1 for a Race ('R') or Sprint ('Sprint').
        Each row: {'driver_code', 'driver_number', 'position', 'points', 'status', 'laps'}.
        """
        year = int(year)
        round_num = int(round_num)
        key = (year, round_num, session_type)
        if key in self._results_mem_cache:
            return self._results_mem_cache[key]

        endpoint = "sprint" if session_type == "Sprint" else "results"
        field = "SprintResults" if session_type == "Sprint" else "Results"
        url = f"{JOLPICA_BASE_URL}/{year}/{round_num}/{endpoint}.json"
        data = self._get_json(
            url,
            params={"limit": 100},
            provider="jolpica",
            cache_key=f"{endpoint}_{year}_r{round_num}",
            ttl_seconds=None,  # Empty responses are not cached; once populated, results are immutable
        )
        if not data:
            return []

        races = data.get("MRData", {}).get("RaceTable", {}).get("Races", [])
        if not races:
            return []

        raw_results = races[0].get(field, [])
        parsed = []
        for r in raw_results:
            driver = r.get("Driver", {})
            code = driver.get("code") or driver.get("driverId", "")
            num_str = r.get("number") or driver.get("permanentNumber")
            try:
                driver_num = int(num_str) if num_str is not None else None
            except ValueError:
                driver_num = None
            parsed.append({
                "driver_code": code,
                "driver_number": driver_num,
                "position": int(r["position"]) if str(r.get("position", "")).isdigit() else None,
                "points": float(r.get("points", 0.0)),
                "status": r.get("status", ""),
                "laps": int(r.get("laps", 0)) if str(r.get("laps", "")).isdigit() else 0,
            })

        if parsed:
            self._results_mem_cache[key] = parsed
        return parsed

    # ------------------------------------------------------------------
    # OpenF1 methods
    # ------------------------------------------------------------------
    def get_openf1_sessions(self, year):
        """Return all Race & Sprint sessions for `year` from OpenF1."""
        year = int(year)
        if year < 2023:
            return []
        if year in self._openf1_sessions_mem_cache:
            return self._openf1_sessions_mem_cache[year]

        current_year = datetime.datetime.utcnow().year
        ttl = None if year < current_year else 3 * 3600
        url = f"{OPENF1_BASE_URL}/sessions"
        data = self._get_json(
            url,
            params={"year": year, "session_type": "Race"},
            provider="openf1",
            cache_key=f"sessions_{year}_race",
            ttl_seconds=ttl,
        )
        if not isinstance(data, list):
            return []

        if data:
            self._openf1_sessions_mem_cache[year] = data
        return data

    def find_openf1_session(self, year, event, session_type="R"):
        """Match a Jolpica event dict to its corresponding OpenF1 session dict."""
        sessions = self.get_openf1_sessions(year)
        if not sessions:
            return None

        target_name = "Sprint" if session_type == "Sprint" else "Race"
        target_date_str = event["sprint_date"] if session_type == "Sprint" else event["date"]
        if not target_date_str:
            return None

        target_date = datetime.date.fromisoformat(target_date_str)
        candidates = []
        for s in sessions:
            if s.get("session_name") != target_name:
                continue
            ds = s.get("date_start")
            if not ds:
                continue
            try:
                s_date = datetime.date.fromisoformat(ds[:10])
            except ValueError:
                continue
            diff_days = abs((s_date - target_date).days)
            if diff_days <= 2:
                candidates.append((diff_days, s))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def get_openf1_endpoint(self, endpoint, session_key, cache_empty=False):
        """Fetch an OpenF1 session-scoped endpoint with permanent caching once populated."""
        url = f"{OPENF1_BASE_URL}/{endpoint}"
        data = self._get_json(
            url,
            params={"session_key": session_key},
            provider="openf1",
            cache_key=f"{endpoint}_sk{session_key}",
            ttl_seconds=None,
            cache_empty=cache_empty,
        )
        return data if isinstance(data, list) else []


def _parse_utc_datetime(date_str, time_str="14:00:00Z"):
    """Parse Jolpica date + time strings into a naive UTC datetime."""
    if not date_str:
        return None
    t = (time_str or "14:00:00Z").rstrip("Z")
    try:
        return datetime.datetime.fromisoformat(f"{date_str}T{t}")
    except ValueError:
        return datetime.datetime.fromisoformat(f"{date_str}T14:00:00")


def _parse_iso_ts(ts_str):
    """Parse OpenF1 ISO-8601 timestamp string to UTC pandas Timestamp."""
    if not ts_str:
        return pd.NaT
    return pd.to_datetime(ts_str, utc=True, errors="coerce")


def _build_lap_dataframe(laps_raw, stints_raw, pits_raw, positions_raw, official_total_laps=None):
    """
    Reconstruct a FastF1-equivalent laps DataFrame with columns:
    Driver, LapNumber, Compound, Position, IsPitIn, IsPitOut
    """
    if not laps_raw:
        return pd.DataFrame()

    df_laps = pd.DataFrame(laps_raw)
    if "driver_number" not in df_laps.columns or "lap_number" not in df_laps.columns:
        return pd.DataFrame()

    df_laps = df_laps[["driver_number", "lap_number", "date_start", "lap_duration", "is_pit_out_lap"]].copy()
    df_laps["driver_number"] = df_laps["driver_number"].astype(int)
    df_laps["lap_number"] = df_laps["lap_number"].astype(int)
    df_laps["is_pit_out_lap"] = df_laps["is_pit_out_lap"].fillna(False).astype(bool)
    df_laps["date_start_ts"] = pd.to_datetime(df_laps["date_start"], utc=True, errors="coerce")
    df_laps["lap_duration"] = pd.to_numeric(df_laps["lap_duration"], errors="coerce")

    # Drop any spurious post-checkered-flag cooldown lap entries beyond official race distance
    if official_total_laps and official_total_laps > 0:
        df_laps = df_laps[df_laps["lap_number"] <= official_total_laps]

    df_laps = df_laps.sort_values(["driver_number", "lap_number"]).reset_index(drop=True)

    # Compute lap end timestamp:
    # Primary: start timestamp of the driver's next lap (exact finish-line crossing time, works for Lap 1 too!)
    # Fallback (final lap or missing next start): date_start + lap_duration
    next_start = df_laps.groupby("driver_number")["date_start_ts"].shift(-1)
    next_lap_num = df_laps.groupby("driver_number")["lap_number"].shift(-1)
    consecutive = next_lap_num == (df_laps["lap_number"] + 1)

    computed_end = df_laps["date_start_ts"] + pd.to_timedelta(df_laps["lap_duration"], unit="s")
    df_laps["lap_end_ts"] = next_start.where(consecutive & next_start.notna(), computed_end)

    # Map pit-in laps from /pit, plus infer pit-in on (lap - 1) for any pit-out lap > 1 (e.g. red flag pit stops)
    pit_in_set = set()
    drv_pits = defaultdict(set)
    for p in pits_raw or []:
        drv = p.get("driver_number")
        ln = p.get("lap_number")
        if drv is not None and ln is not None:
            d_int, l_int = int(drv), int(ln)
            if not official_total_laps or l_int <= official_total_laps:
                pit_in_set.add((d_int, l_int))
                drv_pits[d_int].add(l_int)

    for drv, ln, is_out in zip(df_laps["driver_number"], df_laps["lap_number"], df_laps["is_pit_out_lap"]):
        if is_out and ln > 1:
            pit_in_set.add((drv, ln - 1))
            drv_pits[drv].add(ln - 1)

    df_laps["IsPitIn"] = [
        (drv, ln) in pit_in_set
        for drv, ln in zip(df_laps["driver_number"], df_laps["lap_number"])
    ]
    df_laps["IsPitOut"] = df_laps["is_pit_out_lap"]

    # Map tire compounds from stints (with pit-stop alignment when raw stint lap ranges are malformed, e.g. aborted start/red flag)
    drv_stints = defaultdict(list)
    for st in stints_raw or []:
        drv = st.get("driver_number")
        comp = st.get("compound")
        if drv is not None and comp:
            drv_stints[int(drv)].append(st)

    drv_max_lap = df_laps.groupby("driver_number")["lap_number"].max().to_dict()
    compound_map = {}
    for drv, s_list in drv_stints.items():
        s_list.sort(key=lambda x: int(x.get("stint_number") or 0))
        p_list = sorted(drv_pits.get(drv, set()))
        max_l = drv_max_lap.get(drv, 0)

        # Check if raw stint boundaries disagree with observed pit stops (common after red-flagged starts)
        raw_ends = [int(s["lap_end"]) for s in s_list[:-1] if s.get("lap_end") is not None]
        if p_list and len(s_list) == len(p_list) + 1 and raw_ends != p_list:
            starts = [1] + [p + 1 for p in p_list]
            ends = p_list + [max_l]
            for st, st_l, en_l in zip(s_list, starts, ends):
                comp_upper = str(st["compound"]).upper()
                for ln in range(st_l, en_l + 1):
                    compound_map[(drv, ln)] = comp_upper
        else:
            for st in s_list:
                l_start = st.get("lap_start")
                l_end = st.get("lap_end")
                if l_start is None or l_end is None:
                    continue
                comp_upper = str(st["compound"]).upper()
                for ln in range(int(l_start), int(l_end) + 1):
                    compound_map[(drv, ln)] = comp_upper

    df_laps["Compound"] = [
        compound_map.get((drv, ln), "")
        for drv, ln in zip(df_laps["driver_number"], df_laps["lap_number"])
    ]

    # Join running position at lap_end_ts from /position using per-driver merge_asof
    # Add a small +1.5s grace window so position updates emitted right at the timing line attach to the completed lap
    df_laps["Position"] = np.nan
    if positions_raw:
        df_pos = pd.DataFrame(positions_raw)
        if {"driver_number", "date", "position"}.issubset(df_pos.columns):
            df_pos = df_pos[["driver_number", "date", "position"]].copy()
            df_pos["driver_number"] = df_pos["driver_number"].astype(int)
            df_pos["pos_ts"] = pd.to_datetime(df_pos["date"], utc=True, errors="coerce")
            df_pos["position"] = pd.to_numeric(df_pos["position"], errors="coerce")
            df_pos = df_pos.dropna(subset=["pos_ts", "position"]).sort_values("pos_ts")

            valid_mask = df_laps["lap_end_ts"].notna()
            if valid_mask.any():
                left = df_laps.loc[valid_mask, ["driver_number", "lap_number", "lap_end_ts"]].copy()
                left["lookup_ts"] = left["lap_end_ts"] + pd.Timedelta(seconds=1.5)
                left = left.sort_values("lookup_ts")

                merged = pd.merge_asof(
                    left,
                    df_pos[["driver_number", "pos_ts", "position"]],
                    left_on="lookup_ts",
                    right_on="pos_ts",
                    by="driver_number",
                    direction="backward",
                )
                pos_lookup = {
                    (int(row.driver_number), int(row.lap_number)): row.position
                    for row in merged.itertuples(index=False)
                }
                df_laps["Position"] = [
                    pos_lookup.get((drv, ln), np.nan)
                    for drv, ln in zip(df_laps["driver_number"], df_laps["lap_number"])
                ]

    df_laps = df_laps.rename(columns={"driver_number": "Driver", "lap_number": "LapNumber"})
    return df_laps[["Driver", "LapNumber", "Compound", "Position", "IsPitIn", "IsPitOut"]]


def calculate_session_metrics(client, year, event, session_type="R", legacy_retirement_regex=False):
    """
    Calculate the 6 race/sprint metrics for an event using OpenF1 + Jolpica-F1.

    Returns a dict with canonical keys:
        overtakes_per_lap, lead_changes, weather_volatility_index,
        safety_car_laps_ratio, pit_stop_intensity, retirement_rate
    or None if session data is not yet available.
    """
    try:
        of_session = client.find_openf1_session(year, event, session_type)
        if not of_session:
            logger.warning(f"No OpenF1 session found for {year} {event['gp_name']} ({session_type}).")
            return None

        session_key = of_session["session_key"]
        laps_raw = client.get_openf1_endpoint("laps", session_key)
        if not laps_raw:
            logger.warning(f"No OpenF1 lap data yet for {year} {event['gp_name']} ({session_type}, sk={session_key}).")
            return None

        stints_raw = client.get_openf1_endpoint("stints", session_key)
        pits_raw = client.get_openf1_endpoint("pit", session_key, cache_empty=False)
        rc_raw = client.get_openf1_endpoint("race_control", session_key)
        pos_raw = client.get_openf1_endpoint("position", session_key)

        # Determine official lap count from Jolpica results (or OpenF1 session_result) to ignore post-flag cooldown laps
        official_results = client.get_session_results(year, event["round"], session_type)
        official_total_laps = max((r.get("laps", 0) for r in official_results), default=0) if official_results else 0
        if not official_total_laps:
            of_results = client.get_openf1_endpoint("session_result", session_key)
            official_total_laps = max(
                (int(r.get("number_of_laps") or 0) for r in of_results), default=0
            ) if of_results else 0

        laps = _build_lap_dataframe(
            laps_raw, stints_raw, pits_raw, pos_raw, official_total_laps=official_total_laps or None
        )
        if laps.empty:
            return None

        total_laps = official_total_laps if official_total_laps > 0 else int(laps["LapNumber"].max())
        if total_laps <= 0:
            return None

        # 1. Weather Volatility (fraction of driver-laps on INTERMEDIATE or WET tires)
        wet_laps = laps[laps["Compound"].isin(["INTERMEDIATE", "WET"])].shape[0]
        weather_score = (wet_laps / laps.shape[0]) if laps.shape[0] > 0 else 0.0

        # 2. Safety Car / VSC (exact parity with FastF1: SC count + VSC count / total_laps)
        safety_score = 0.0
        if rc_raw:
            msgs = pd.Series([str(m.get("message", "")) for m in rc_raw])
            sc = msgs.str.contains("SAFETY CAR", case=False, na=False).sum()
            vsc = msgs.str.contains("VIRTUAL SAFETY CAR", case=False, na=False).sum()
            safety_score = float(sc + vsc) / total_laps

        # 3. Lead Changes
        leaders = laps[laps["Position"] == 1].sort_values("LapNumber")
        lap_leaders = leaders.drop_duplicates(subset=["LapNumber"], keep="last")

        lead_changes = 0
        previous_leader = None
        for _, row in lap_leaders.iterrows():
            curr_leader = row["Driver"]
            if previous_leader is not None and curr_leader != previous_leader:
                lead_changes += 1
            previous_leader = curr_leader

        lead_changes_normalized = lead_changes / total_laps

        # 4. Overtakes (Approximation: position changes excluding pit in/out transitions)
        laps_sorted = laps.sort_values(["Driver", "LapNumber"]).copy()
        laps_sorted["PrevPos"] = laps_sorted.groupby("Driver")["Position"].shift(1)
        laps_sorted["PrevPitIn"] = laps_sorted.groupby("Driver")["IsPitIn"].shift(1, fill_value=False)
        laps_sorted["PrevPitOut"] = laps_sorted.groupby("Driver")["IsPitOut"].shift(1, fill_value=False)

        valid_transition = (
            (~laps_sorted["IsPitIn"])
            & (~laps_sorted["IsPitOut"])
            & (~laps_sorted["PrevPitIn"])
            & (~laps_sorted["PrevPitOut"])
        )

        laps_sorted["PosChange"] = (laps_sorted["Position"] - laps_sorted["PrevPos"]).fillna(0).abs()
        total_pos_change = laps_sorted.loc[valid_transition, "PosChange"].sum()
        overtakes_proxy = float(total_pos_change) / total_laps

        # 5. Pit Stop Intensity
        pit_stops = int(laps["IsPitIn"].sum())
        pit_intensity = pit_stops / total_laps if total_laps > 0 else 0.0

        # 6. Retirement Rate (prefer Jolpica official results, fall back to OpenF1 session_result)
        retirement_rate = _calculate_retirement_rate(
            client, year, event["round"], session_type, session_key, legacy_retirement_regex
        )

        return {
            "overtakes_per_lap": float(overtakes_proxy),
            "lead_changes": float(lead_changes_normalized),
            "weather_volatility_index": float(weather_score),
            "safety_car_laps_ratio": float(safety_score),
            "pit_stop_intensity": float(pit_intensity),
            "retirement_rate": float(retirement_rate),
        }

    except Exception as e:
        logger.error(f"Error processing metrics for {year} {event.get('gp_name')} ({session_type}): {e}")
        return None


def _calculate_retirement_rate(client, year, round_num, session_type, session_key, legacy_regex=False):
    results = client.get_session_results(year, round_num, session_type)
    if results:
        started = len(results)
        pattern = LEGACY_FINISHED_STATUS_RE if legacy_regex else FINISHED_STATUS_RE
        finished = sum(1 for r in results if pattern.search(r.get("status", "")))
        return (started - finished) / started if started > 0 else 0.0

    # Fallback to OpenF1 /session_result if Jolpica hasn't published yet
    of_results = client.get_openf1_endpoint("session_result", session_key)
    if of_results:
        started = len(of_results)
        retired = sum(1 for r in of_results if r.get("dnf") or r.get("dns") or r.get("dsq"))
        return retired / started if started > 0 else 0.0

    return 0.0


# ------------------------------------------------------------------
# Championship Standings Metrics (via Jolpica-F1)
# ------------------------------------------------------------------
def get_remaining_points(schedule, current_round, session_type):
    """Calculate max remaining driver points after (current_round, session_type)."""
    remaining_gps = 0
    remaining_sprints = 0
    for event in schedule:
        if event["round"] > current_round:
            remaining_gps += 1
            if event["has_sprint"]:
                remaining_sprints += 1
    if session_type == "Sprint":
        remaining_gps += 1
    return (remaining_gps * 26) + (remaining_sprints * 8)


def calculate_standings_metrics(client, year, round_num, session_type, schedule=None, include_gaps=False):
    """
    Reconstruct season standings up through (round_num, session_type) using Jolpica-F1
    (with OpenF1 session_result fallback for just-finished sessions).
    """
    if schedule is None:
        schedule = client.get_schedule(year)

    driver_points = defaultdict(float)
    gap_before, leader_before = 0.0, None

    for event in schedule:
        r_num = event["round"]
        if r_num > round_num:
            break

        sessions = ["Sprint", "R"] if event["has_sprint"] else ["R"]
        for s_type in sessions:
            if r_num == round_num and session_type == "Sprint" and s_type == "R":
                break

            if r_num == round_num and s_type == session_type:
                sorted_st = sorted(driver_points.items(), key=lambda x: x[1], reverse=True)
                gap_before = sorted_st[0][1] - sorted_st[1][1] if len(sorted_st) >= 2 else 0.0
                leader_before = sorted_st[0][0] if len(sorted_st) >= 1 else None

            results = client.get_session_results(year, r_num, s_type)
            if results:
                for r in results:
                    drv = r["driver_code"]
                    driver_points[drv] += float(r["points"])
            else:
                # Fallback to OpenF1 session_result if Jolpica hasn't posted yet
                of_sess = client.find_openf1_session(year, event, s_type)
                if of_sess:
                    of_res = client.get_openf1_endpoint("session_result", of_sess["session_key"])
                    if of_res:
                        # Map driver numbers to driver codes using /drivers if needed
                        drivers_raw = client.get_openf1_endpoint("drivers", of_sess["session_key"])
                        num_to_code = {
                            int(d["driver_number"]): d.get("name_acronym", str(d["driver_number"]))
                            for d in drivers_raw
                            if d.get("driver_number") is not None
                        }
                        for r in of_res:
                            num = r.get("driver_number")
                            pts = float(r.get("points") or 0.0)
                            if num is not None:
                                code = num_to_code.get(int(num), str(num))
                                driver_points[code] += pts

            if r_num == round_num and s_type == session_type:
                break

    sorted_after = sorted(driver_points.items(), key=lambda x: x[1], reverse=True)
    gap_after = sorted_after[0][1] - sorted_after[1][1] if len(sorted_after) >= 2 else 0.0
    leader_after = sorted_after[0][0] if len(sorted_after) >= 1 else None

    rem_pts_before = get_remaining_points(schedule, round_num, session_type)
    rem_pts_before += 8 if session_type == "Sprint" else 26
    rem_pts_after = get_remaining_points(schedule, round_num, session_type)

    championship_active = 1
    if len(driver_points) >= 2:
        championship_active = 1 if gap_before <= rem_pts_before else 0

    title_clinched = 0
    if championship_active == 1 and gap_after > rem_pts_after:
        title_clinched = 1

    leader_changed = 0
    if leader_before is not None and leader_after != leader_before:
        leader_changed = 1

    tension = 0.0
    if championship_active == 1 and rem_pts_before > 0:
        tension = 1.0 - (gap_before / rem_pts_before)

    result = {
        "championship_active": championship_active,
        "championship_tension": round(tension, 4),
        "title_clinched": title_clinched,
        "leader_changed": leader_changed,
    }
    if include_gaps:
        result["gap_before"] = gap_before
        result["gap_after"] = gap_after
    return result
