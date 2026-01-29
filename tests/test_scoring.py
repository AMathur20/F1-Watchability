import unittest
import json
import os
import sys

# Add pyscript to path to import logic if needed, 
# but getting functions out of pyscript files can be tricky due to globals.
# For this test, we will duplicate the SCORING FORMULA logic to verify the MATH 
# ensures our inputs result in the expected outputs.
# Ideally, we would refactor f1_watchability.py to have a pure `calculate_score(...)` function 
# that we can import, but Pyscript structure makes imports hard.
# So we will implementing a "shadow" test that verifies the logic we intend to use.

class TestWatchabilityScoring(unittest.TestCase):
    
    def setUp(self):
        # Load the weights we are shipping
        weights_path = os.path.join(os.path.dirname(__file__), '../pyscript/weights.json')
        with open(weights_path, 'r') as f:
            self.weights_data = json.load(f)
            
    def calculate_score(self, overtakes, lead_changes, weather, safety_car):
        w = self.weights_data['weights']
        
        score = (
            (overtakes * w.get('overtakes_per_lap', 0)) +
            (lead_changes * w.get('lead_changes', 0)) +
            (weather * w.get('weather_volatility_index', 0)) +
            (safety_car * w.get('safety_car_laps_ratio', 0)) +
            w.get('base_score', 0)
        )
        return max(0, min(10, round(score, 1)))

    def test_boring_race(self):
        # 0 overtakes, 0 everything
        score = self.calculate_score(0, 0, 0, 0)
        print(f"Boring Race Score: {score}")
        self.assertLess(score, 5.0) # Should be low
        
    def test_exciting_race(self):
        # High overtakes (e.g. 1.5 per lap is massive), Rain, SC
        score = self.calculate_score(1.5, 0.1, 0.5, 0.2)
        print(f"Exciting Race Score: {score}")
        self.assertGreater(score, 7.0) # Should be high

    def test_thresholds(self):
        t = self.weights_data['thresholds']
        self.assertEqual(t['full_race'], 8.0)
        self.assertEqual(t['race_in_30'], 5.0)

if __name__ == '__main__':
    unittest.main()
