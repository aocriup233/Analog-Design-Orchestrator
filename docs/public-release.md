# Public release boundary

ADO publishes technology-independent software under MIT. A software license
does not grant rights to any user-supplied process library or characterization.
The repository's gm/Id examples are synthetic; no real PDK is required to run tests.

Keep model decks, lookup data, extraction mappings, legal PDK ranges, site
configuration, credentials, logs and derived circuit results in project `private/`
or other ignored runtime directories. `private.example/` contains empty interface
templates only. Do not force-add private files. Never publish a project directory
or raw `git archive` as a Python distribution.

Before publishing:

1. Review `git diff --cached` and `git ls-files`. Inspect reachable Git history
   as well as the current tree for private data. Ignore rules do not remove history.
2. Install the optional dependencies and run synthetic tests:
   `python -m pip install -e '.[gmid]'` and
   `python -m unittest discover -s tests -q`.
3. Build fresh artifacts: `python -m build --outdir dist`.
4. Run `python scripts/check_public_release.py dist/NAME.whl dist/NAME.tar.gz`.
   This validates actual archive membership against a narrow allowlist, without
   extracting it. Wheel/sdist contain code and package metadata only; source
   distributions intentionally exclude project examples, test fixtures and site
   templates. Repository documentation/examples remain available on GitHub.
5. Review source contents and metadata too. An archive allowlist prevents accidental
   data-file inclusion; it cannot detect arbitrary secrets embedded in Python or prose.

CI repeats gm/Id tests, builds both artifacts with a private synthetic canary in
the checkout, and checks their members. Publish only the reviewed artifacts.
If private content was ever committed, stop publication and resolve the history
and any exposed credentials before pushing. Do not paste private findings into
public issues or pull request descriptions.
