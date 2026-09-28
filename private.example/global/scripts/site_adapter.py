"""Copy into private/global/scripts and implement for a site scheduler.

Configure workflow.role_commands.simulation to call your adapter CLI. It must
write simulation_result.json using the public run contract. Keep credentials in
the site's secure environment, not in this script.
"""


def submit(run_dir):
    raise NotImplementedError("Configure this site's submission mechanism")


def collect(run_dir, job_id):
    raise NotImplementedError("Configure this site's result retrieval mechanism")


def cleanup_remote(run_dir, job_id):
    raise NotImplementedError("Configure this site's remote cleanup mechanism")
