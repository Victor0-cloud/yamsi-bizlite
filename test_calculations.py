import unittest
from calculations import poultry, water, commission

class Calculations(unittest.TestCase):
    def farm(self):
        return {"crates_produced":26, "sales":[{"quantity":26,"unit_price":5200}],
                "feed_usage":[{"bags_used":4.5,"inventory_cost_per_bag":14300}],
                "employees":[{"monthly_salary":57000},{"monthly_salary":37000},{"monthly_salary":30000}],
                "payroll_days":30,"period_days":1,"other_costs":[]}
    def test_reference_poultry(self):
        result=poultry(self.farm())
        self.assertEqual(result["revenue"],"135200.00")
        self.assertEqual(result["feed_cost"],"64350.00")
        self.assertEqual(result["labor_cost"],"4133.33")
        self.assertEqual(result["revenue_less_recorded_cost"],"66716.67")
    def test_production_is_not_sales(self):
        data=self.farm(); data["sales"]=[]
        self.assertEqual(poultry(data)["revenue"],"0.00")
    def test_zero_is_preserved(self):
        data=self.farm(); data["crates_produced"]=0; data["feed_usage"][0]["bags_used"]=0
        result=poultry(data)
        self.assertIsNone(result["production_cost_per_crate"])
        self.assertEqual(result["feed_cost"],"0.00")
    def test_variable_prices(self):
        data=self.farm(); data["sales"]=[{"quantity":10,"unit_price":5200},{"quantity":8,"unit_price":5500},{"quantity":3,"unit_price":5300}]
        self.assertEqual(poultry(data)["revenue"],"111900.00")
    def test_warri(self):
        result=water({"bags_per_kg":40,"film_cost_per_kg":4000,"packing_cost":23,"fuel_cost":15000,"fuel_output_bags":600,"operator_per_bag":0,"sale_price":350,"commission_per_bag":60})
        self.assertEqual(result["remaining_before_other_costs"],"142.00")
    def test_partial_collection(self):
        self.assertEqual(commission({"sale_total":350,"cumulative_paid":175,"bags":1,"rate":60,"already_released":0})["newly_releasable"],"30.00")
    def test_repeated_release(self):
        self.assertEqual(commission({"sale_total":350,"cumulative_paid":175,"bags":1,"rate":60,"already_released":30})["newly_releasable"],"0.00")
    def test_invalid(self):
        for value in [-1,"NaN","Infinity",None]:
            data=self.farm(); data["crates_produced"]=value
            with self.assertRaises(ValueError): poultry(data)
    def test_zero_denominator(self):
        data=self.farm(); data["payroll_days"]=0
        with self.assertRaises(ValueError): poultry(data)

if __name__ == "__main__": unittest.main()
