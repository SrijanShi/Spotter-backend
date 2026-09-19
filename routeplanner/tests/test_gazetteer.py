from django.test import SimpleTestCase

from routeplanner.services.gazetteer import name_aliases, normalize_place_name


class NormalizationTests(SimpleTestCase):
    def test_strips_one_legal_designator_only(self):
        # Stripping designators repeatedly would leave "OKLAHOMA".
        self.assertEqual(normalize_place_name("Oklahoma City city"), "OKLAHOMA CITY")
        self.assertEqual(normalize_place_name("New York city"), "NEW YORK")
        self.assertEqual(normalize_place_name("Big Cabin town"), "BIG CABIN")
        self.assertEqual(normalize_place_name("Abanda CDP"), "ABANDA")

    def test_strips_balance_before_the_designator(self):
        self.assertEqual(normalize_place_name("Indianapolis city (balance)"), "INDIANAPOLIS")
        self.assertEqual(normalize_place_name("Nashville-Davidson metropolitan government (balance)"),
                         "NASHVILLE-DAVIDSON")

    def test_aliases_cover_the_spellings_the_price_file_uses(self):
        self.assertIn("MCCALLA", name_aliases("MC CALLA"))
        self.assertIn("DEFOREST", name_aliases("DE FOREST"))
        self.assertIn("LASALLE", name_aliases("LA SALLE"))
        self.assertIn("SAINT LOUIS", name_aliases("ST. LOUIS"))
        self.assertIn("ST. LOUIS", name_aliases("SAINT LOUIS"))
        self.assertIn("MOUNT JACKSON", name_aliases("MT. JACKSON"))

    def test_aliases_handle_consolidated_names(self):
        aliases = name_aliases(normalize_place_name("Augusta-Richmond County consolidated government"))
        self.assertIn("AUGUSTA", aliases)

    def test_trailing_city_is_an_alias(self):
        # Texas has a "Town of Pecos City"; the price file just says "Pecos".
        self.assertIn("PECOS", name_aliases("PECOS CITY"))

    def test_empty_input_is_safe(self):
        self.assertEqual(normalize_place_name(""), "")
        self.assertEqual(name_aliases(""), set())
