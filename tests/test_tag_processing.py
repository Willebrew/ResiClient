import unittest

from tag_6c import (
    classify_tag_output,
    encode_6ctoc,
    inspect_6c_candidate,
    match_6ctoc,
)


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

    def test_values_at_or_below_15_characters_lose_last_two(self):
        observed = {
            "DFW.0683855844": "DFW.06838558",
            "DNT.1268287922": "DNT.12682879",
            "123456789012345": "1234567890123",
            "0058": "00",
            "JACK": "JA",
        }
        for raw_value, expected in observed.items():
            with self.subTest(raw_value=raw_value):
                classified = classify_tag_output(raw_value)
                self.assertIsNotNone(classified)
                self.assertEqual(classified.lookup_value, expected)
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

    def test_values_over_15_characters_must_decode_as_known_6c(self):
        for value in (
            "1234567890123456",
            "280083348888191120441742",
            "3000DEE0000CF212A4423000709F",
        ):
            with self.subTest(value=value):
                self.assertIsNone(classify_tag_output(value))

    def test_undocumented_hctra_candidate_emits_raw_diagnostics(self):
        value = "3000DEE0000CF212A4423000709F"
        details = inspect_6c_candidate(value)

        self.assertEqual(details["raw"], value)
        self.assertEqual(details["hexLength"], 28)
        self.assertEqual(details["pc"], "3000")
        self.assertEqual(details["pcDecimal"], 12288)
        self.assertEqual(details["uii"], "DEE0000CF212A4423000709F")
        self.assertEqual(
            details["uiiWords16"],
            ["DEE0", "000C", "F212", "A442", "3000", "709F"],
        )
        self.assertEqual(details["dsfidHex"], "0xDE")
        self.assertFalse(details["is6cToc"])
        self.assertIn("tocAgencyCode", details)
        self.assertIn("tocSerialNumber", details)

    def test_malformed_pc_word_does_not_raise(self):
        # UII is valid hex but the PC word contains non-hex characters. This
        # must be reported as a diagnostic error instead of propagating a
        # ValueError up into the gateway read loop.
        value = "ZZZZDEE0000CF212A4423000709F"
        details = inspect_6c_candidate(value)

        self.assertEqual(details["raw"], value)
        self.assertEqual(details["hexLength"], 28)
        self.assertIn("error", details)
        self.assertNotIn("uii", details)

    def test_unknown_and_reserved_agencies_are_discarded(self):
        unknown = encode_6ctoc("4094 0000000001", include_pc=True)
        reserved = encode_6ctoc("0 0000000001", include_pc=True)
        self.assertIsNone(classify_tag_output(unknown))
        self.assertIsNone(classify_tag_output(reserved))


if __name__ == "__main__":
    unittest.main()
