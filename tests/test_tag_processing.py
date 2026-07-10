import unittest

from tag_6c import classify_tag_output, encode_6ctoc, match_6ctoc


PHOTO_NTTA_SERIALS = (
    "0001392446",
    "0000480314",
    "0000845310",
    "0000637468",
    "0000225420",
)


class TagProcessingTests(unittest.TestCase):
    def test_all_five_photo_tags_across_current_ntta_agency_codes(self):
        for serial in PHOTO_NTTA_SERIALS:
            for agency_code in (41, 53, 69):
                with self.subTest(serial=serial, agency_code=agency_code):
                    epc = encode_6ctoc(
                        f"{agency_code} {serial}", include_pc=True
                    )
                    classified = classify_tag_output(epc)

                    self.assertIsNotNone(classified)
                    self.assertEqual(classified.display_value, f"NTTA {serial}")
                    self.assertEqual(classified.protocol, "6c")
                    self.assertTrue(match_6ctoc(f"NTTA {serial}", epc))

    def test_all_florida_turnpike_agency_codes_match_one_acronym(self):
        serial = "0000123456"
        for agency_code in (35, 64, 65):
            with self.subTest(agency_code=agency_code):
                epc = encode_6ctoc(
                    f"{agency_code} {serial}", include_pc=True
                )
                classified = classify_tag_output(epc)

                self.assertIsNotNone(classified)
                self.assertEqual(classified.display_value, f"FTE {serial}")
                self.assertTrue(match_6ctoc(f"FTE {serial}", epc))

    def test_legacy_transcore_formats_are_preserved(self):
        for value in ("DFW.06956066", "DNT.15652347", "0058", "1234567890"):
            with self.subTest(value=value):
                classified = classify_tag_output(value)
                self.assertIsNotNone(classified)
                self.assertEqual(classified.lookup_value, value)
                self.assertEqual(classified.protocol, "legacy")

    def test_live_legacy_reader_suffixes_are_removed(self):
        observed = {
            "DFW.0683855844": "DFW.06838558",
            "DFW.039806621F": "DFW.03980662",
            "DFW.0695606620": "DFW.06956066",
            "DNT.1268287922": "DNT.12682879",
            "DNT.15295695E0": "DNT.15295695",
        }
        for raw_value, stored_value in observed.items():
            with self.subTest(raw_value=raw_value):
                classified = classify_tag_output(raw_value)
                self.assertIsNotNone(classified)
                self.assertEqual(classified.lookup_value, stored_value)
                self.assertEqual(classified.display_value, stored_value)
                self.assertEqual(classified.protocol, "legacy")

    def test_agency_prefixed_legacy_identifier_is_preserved(self):
        value = "OOCEA0779782"
        classified = classify_tag_output(value)
        self.assertIsNotNone(classified)
        self.assertEqual(classified.lookup_value, value)
        self.assertEqual(classified.protocol, "legacy")

    def test_long_non_6c_output_is_discarded(self):
        for value in ("12345678901", "280083348888", "3400301854AA"):
            with self.subTest(value=value):
                self.assertIsNone(classify_tag_output(value))

    def test_unknown_and_reserved_agencies_are_discarded(self):
        unknown = encode_6ctoc("4094 0000000001", include_pc=True)
        reserved = encode_6ctoc("0 0000000001", include_pc=True)
        self.assertIsNone(classify_tag_output(unknown))
        self.assertIsNone(classify_tag_output(reserved))


if __name__ == "__main__":
    unittest.main()
