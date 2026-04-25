# XJTLU Modules

This repository stores scraped JSON data from the XJTLU module catalogue.

## Scraper

The scraper reads:

- `https://modules.xjtlu.edu.cn/dept`
- `https://modules.xjtlu.edu.cn/dom?dom_code=<Domain Code>`
- `https://modules.xjtlu.edu.cn/mod?mod_code=<Mod Code>&psl_code=<Semester>`

It writes module detail JSON files into folders named:

```text
<Domain Code>|<Department Code> - <Full Name>/<Module Code>.json
```

If a module code has multiple catalogue rows, the file contains an `offerings` array with each academic year and semester entry.

Run it locally with:

```bash
python scripts/scrape_modules.py --output . --prune --workers 6
```

The GitHub Actions workflow runs daily at `16:00 UTC`, which is midnight in `Asia/Shanghai`, and commits catalogue changes when the generated files differ.

When a previously scraped module is no longer present in the current catalogue, the scraper moves its JSON file to `archived/` with the original folder structure preserved.
