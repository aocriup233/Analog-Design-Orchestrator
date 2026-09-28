"""Template for a private LSF transport/scheduler adapter.

Copy into a user's private project, implement each method with reusable local
functions, then configure simulation.backend="lsf" and
simulation.adapter="private/agents/simulation/scripts/site_lsf_adapter.py:create".
No passwords should appear in config, argv, returned dicts, or run artifacts.
"""


class SiteLSFAdapter:
    def stage(self, run, config, plan):
        """Transfer exact input files; return input_sha256=plan['deck_sha256']."""
        raise NotImplementedError

    def submit(self, run, config, staged):
        """Submit once; return {'state': 'SUBMITTED', 'job_id': '...'}.

        Use run.name as a site-side idempotency key so a process interruption
        before the local handoff write cannot create duplicate LSF jobs.
        """
        raise NotImplementedError

    def poll(self, run, config, submitted):
        """Return {'state': 'SUBMITTED'|'RUN'|'DONE'|'FAILED', ...}."""
        raise NotImplementedError

    def retrieve(self, run, config, submitted):
        """Download outputs below run; list path, sha256, remote_sha256."""
        raise NotImplementedError

    def verify(self, run, config, submitted, retrieved):
        """Require LSF DONE, simulator success, and parse results.

        Return {'ok': True, 'data': {waveform keys...}, 'metadata': {...}}.
        """
        raise NotImplementedError

    def cleanup(self, run, config, submitted):
        """Remove only verified temporary staging copies; return {'status': 'DONE', ...}."""
        raise NotImplementedError


def create(config, run):
    return SiteLSFAdapter()
