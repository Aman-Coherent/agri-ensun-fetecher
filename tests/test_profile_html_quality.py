"""Quality gates for HTTP profile parsing (description, logo, size label)."""

from __future__ import annotations

import unittest

from src.graphql.profile_normalize import is_valid_profile_description
from src.profile_rsc import extract_logo_from_html


class TestProfileDescriptionQuality(unittest.TestCase):
    def test_rejects_corrupt_rsc(self) -> None:
        self.assertFalse(is_valid_profile_description("$1e"))

    def test_rejects_generic_meta(self) -> None:
        self.assertFalse(
            is_valid_profile_description(
                "Get all information about Acme including their revenue, certificates, employees."
            )
        )


class TestProfileCsvExport(unittest.TestCase):
    def test_employee_range_excel_safe(self) -> None:
        from src.graphql.profile_flatten import format_profile_csv_cell

        self.assertEqual(
            format_profile_csv_cell("employee_range", "1-10"),
            "\t1-10 Employees",
        )
        self.assertEqual(
            format_profile_csv_cell("employee_range", "11-50"),
            "\t11-50 Employees",
        )
        self.assertIsNone(format_profile_csv_cell("employee_range", None))
        self.assertEqual(format_profile_csv_cell("phone", "+44123"), "+44123")

    def test_accepts_real_overview(self) -> None:
        text = (
            "Robinson Brothers is a leading independent chemical manufacturer based in the UK, "
            "specializing in speciality and fine chemicals."
        )
        self.assertTrue(is_valid_profile_description(text))


class TestLogoExtraction(unittest.TestCase):
    def test_extract_logo_by_company_id(self) -> None:
        cid = "67af88f5cdd24f1dfaabe98d"
        html = (
            'self.__next_f.push([1,"a:{\\"logo\\":{\\"url\\":\\"'
            f"https://api.platform-production.ensun.io/file/external-company-logo/{cid}"
            '\\"}}"])\n'
        )
        url = extract_logo_from_html(html, cid)
        self.assertIn(cid, url)
        self.assertIn("external-company-logo", url)


if __name__ == "__main__":
    unittest.main()
