import unittest

from scripts.scrape_modules import ModuleListing, extract_modules


MODULE_HEADERS = """
<thead>
  <tr>
    <th>#</th>
    <th>Domain Code</th>
    <th>Mod Code</th>
    <th>Full Name</th>
    <th>Academic Year</th>
    <th>Semester</th>
  </tr>
</thead>
"""


class ExtractModulesTests(unittest.TestCase):
    def test_extracts_populated_module_table(self) -> None:
        html = f"""
        <table>
          {MODULE_HEADERS}
          <tbody>
            <tr>
              <td>1</td>
              <td>MTH</td>
              <td><a href="/mod?mod_code=MTH101&amp;psl_code=SEM1">MTH101</a></td>
              <td>Calculus</td>
              <td>2026-27</td>
              <td>SEM1</td>
            </tr>
          </tbody>
        </table>
        """

        self.assertEqual(
            extract_modules(html, "https://modules.example"),
            [
                ModuleListing(
                    domain_code="MTH",
                    module_code="MTH101",
                    full_name="Calculus",
                    academic_year="2026-27",
                    semester="SEM1",
                    href="https://modules.example/mod?mod_code=MTH101&psl_code=SEM1",
                )
            ],
        )

    def test_accepts_module_table_with_no_rows(self) -> None:
        html = f"<table>{MODULE_HEADERS}<tbody></tbody></table>"

        self.assertEqual(extract_modules(html, "https://modules.example"), [])

    def test_rejects_page_without_module_table(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "module table not found"):
            extract_modules("<html><body>Service unavailable</body></html>", "https://modules.example")


if __name__ == "__main__":
    unittest.main()
