"""Template for a private LSF transport/scheduler adapter.

Copy into a user's private project, implement each method with reusable local
functions, then configure simulation.backend="lsf" and
simulation.adapter="private/agents/simulation/scripts/site_lsf_adapter.py:create".
No passwords should appear in config, argv, returned dicts, or run artifacts.
S3 supplies _attempt_dir, _run_dir and _run_id in runtime config. New output
must use the exclusive attempt directory, never overwrite retained evidence.
Raise failures.RetryableError(code, no_side_effects=True) only when the call
has finished and no job or uncertain mutation exists. Otherwise reconcile.
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

    def reconcile(self, run, config, staged, intent):
        """Optional: look up run.name in the scheduler after an uncertain submit.

        Return the existing real job_id and state; never create a new job here.
        Delete this method if the site cannot safely implement that lookup.
        """
        raise NotImplementedError

    def poll(self, run, config, submitted):
        """Return {'state': 'SUBMITTED'|'RUN'|'DONE'|'FAILED', ...}."""
        raise NotImplementedError

    def retrieve(self, run, config, submitted):
        """Download below run (the exclusive ATTEMPT OUTPUT directory in S3).

        Use config['_run_id'] or submitted job metadata for remote identity;
        this argument's basename is NOT the original run ID. Relative artifact
        paths resolve against this directory. List sha256 and remote_sha256.
        """
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
