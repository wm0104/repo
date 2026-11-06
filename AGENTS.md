# Repository maintenance

This workspace maintains the APT source at https://wm0104.github.io/repo/.
It builds a static Pages/APT artifact, not an installable package.

## Build and checks

On a host with Python 3, dpkg-deb, dpkg-scanpackages and zstd:

```sh
python3 -m unittest discover -s testsuite -v
python3 scripts/build_repo.py --output _site
```

The build validates all debs, copies `depictions/` into `_site/`, and generates
Packages, compressed indexes and Release. `_site/` is ignored; never commit
those generated files. The current iPhone environment lacks Python 3,
dpkg-scanpackages and zstd, so the complete build/tests must run on a suitable
host or in CI. Native depiction JSON and text can be checked independently;
that is not a Sileo rendering test.

`.github/workflows/refresh.yml` runs tests, audits/repairs package permissions,
builds the site, commits verified permission repairs, and deploys Pages on a
push to main (or workflow_dispatch). Do not invoke the repair scripts locally
for a depiction-only change: existing deb bytes must remain unchanged.

## KeyBridge depiction

Stable package fields for `com.wm0104.keybridge`:

- Depiction: https://wm0104.github.io/repo/depictions/com.wm0104.keybridge/index.html
- SileoDepiction: https://wm0104.github.io/repo/depictions/com.wm0104.keybridge/depiction.json
- Section: Ai插件

There is currently no index metadata override mechanism. The index generator
uses the fields in each deb, so adding pages alone does not attach them to old
versions. The existing 0.0.1/0.0.2 debs have no depiction fields and keep their
original Section. Future packages should include the above fields in their
own build metadata. Connecting existing index records without changing debs
requires a separately approved override limited to this package (and tests
preserving all archive hashes). Never edit generated Packages by hand.

Keep changelogs from 0.0.2 onward, with no dates and no invented 0.0.1 entry.
Each change starts with a literal ASCII hyphen and space: use native
DepictionLabelView text and HTML paragraphs, not Markdown lists.

Do not change README or existing debs for this task. Do not push or publish
without explicit authorization. After an authorized push, check the workflow
and both stable URLs, refresh the source in Sileo and check package details
and the changelog tab; HTML fallback also needs a browser rendering check.
