import unittest

from tag_6c import (
    classify_tag_output,
    encode_6ctoc,
    extract_reader_tag,
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
                self.assertEqual(classified.display_value, "FDTA123456")
                self.assertEqual(classified.lookup_value, "FDTA123456")
                self.assertTrue(match_6ctoc(f"FTE {serial}", epc))

    def test_values_at_or_below_15_characters_lose_last_two(self):
        observed = {
            "DFW.0683855844": "DFW.06838558",
            "DNT.1268287922": "DNT.12682879",
            "123456789012345": "1234567890123",
            "0058": "00",
            "JACKZZ": "JACK",
        }
        for raw_value, expected in observed.items():
            with self.subTest(raw_value=raw_value):
                classified = classify_tag_output(raw_value)
                self.assertIsNotNone(classified)
                self.assertEqual(classified.lookup_value, expected)
                self.assertEqual(classified.protocol, "legacy")

    def test_jack_without_reader_suffix_is_preserved(self):
        classified = classify_tag_output("JACK")
        self.assertIsNotNone(classified)
        self.assertEqual(classified.lookup_value, "JACK")
        self.assertEqual(classified.display_value, "JACK")

    def test_reported_operational_examples_are_preserved(self):
        for value in ("OOCEA222222", "FDTA22222222", "JACK"):
            with self.subTest(value=value):
                classified = classify_tag_output(value)
                self.assertIsNotNone(classified)
                self.assertEqual(classified.lookup_value, value)

    def test_production_reader_trailer_variants_are_extracted(self):
        observed = {
            "#JACK      \r\n": "JACK",
            "#DFW.03881851EF...^?$\r\n": "DFW.03881851EF",
            "#OOCEA0490396588..^I$\r\n": "OOCEA0490396588",
            "#FDTA1135689583.1.^F$\r\n": "FDTA1135689583",
            "#35B03E000024030400AD4ADFB487\r\n": "35B03E000024030400AD4ADFB487",
        }
        for raw_value, expected in observed.items():
            with self.subTest(raw_value=raw_value):
                self.assertEqual(extract_reader_tag(raw_value), expected)

    def test_florida_raw_and_6c_forms_normalize_to_same_fdta_value(self):
        raw_form = classify_tag_output("FDTA1135689583")
        six_c_form = classify_tag_output("35B03E000024030400AD4ADFB487")
        self.assertIsNotNone(raw_form)
        self.assertIsNotNone(six_c_form)
        self.assertEqual(raw_form.lookup_value, "FDTA11356895")
        self.assertEqual(six_c_form.lookup_value, "FDTA11356895")

    def test_cfx_6c_uses_oocea_operational_prefix(self):
        epc = encode_6ctoc("99 0000222222", include_pc=True)
        classified = classify_tag_output(epc)
        self.assertIsNotNone(classified)
        self.assertEqual(classified.lookup_value, "OOCEA222222")

    def test_hctra_code_42_uses_hc6c_label(self):
        value = "35B03E9000000402A001A36BDE9E"
        classified = classify_tag_output(value)
        self.assertIsNotNone(classified)
        self.assertEqual(classified.lookup_value, "HC6C 0000107371")
        self.assertEqual(classified.agency_code, 42)

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
