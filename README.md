# Beamline integration test

This is a github action repository for testing a conda environment from an
artifact against a given beamline ipython profile. It uses blackhole IOC to
attempt to start the beamline ipython profile.

## Beamline-specific configurations
Note that beamline-specific preconditioning actions can be added to `special_config.py`.
## Reading a failed job: `scripts/classify_startup.py`

Every job ends with a **Classify startup result** step (it runs even when the profile step failed) that
writes to the job summary:

- a **verdict** — `PROFILE`, `TOOLS PACKAGE`, `ENVIRONMENT`, or `HARNESS / SERVICES` — with the reason;
- the **real exception**: with ophyd v1 the first thing in the log is often `KeyError: '<signal>'` from
  `ophyd/device.py … __get__`; that is ophyd's lazy-init path, and the failure that matters is the
  *chained* one that follows ("During handling of the above exception…"). The classifier prints that one;
- the innermost frame, the nearest frame inside the profile's `startup/`, and hints when the exception
  names a harness service (Tiled, Redis, Mongo, Kafka, the blackhole IOC / Channel Access);
- a **per-startup-file table** (file, ok/failed, seconds) so you can see how far the profile got;
- an `::error` annotation with the same verdict.

An exit 139 (segfault) *after every startup file executed* is filed as `ENVIRONMENT` (pyepics teardown at
interpreter exit), not as a profile failure.

Run it by hand on a downloaded job log (timestamps/prefixes are tolerated):

```bash
gh api repos/<owner>/gha-beamline-integration-test/actions/jobs/<job-id>/logs > job.log
python3 scripts/classify_startup.py job.log --tla hxn
```

## Services: `services/docker-compose.yml`

Redis (6379 plaintext **and** 6380 TLS), MongoDB, Kafka (single-node KRaft) and Tiled come from one
compose file, started by the `start-dependencies` action (`services-backend: compose`, the default).
The same file reproduces a job's services on a laptop:

```bash
services/certs.sh xf03id1-hxn-redis1.nsls2.bnl.gov      # throwaway Redis TLS cert (SAN = that host + 127.0.0.1)
docker compose -f services/docker-compose.yml up -d --wait
# ports already taken? move them:  REDIS_PLAIN_PORT=16379 docker compose -f services/docker-compose.yml up -d --wait
```

Caddy (TLS front for `tiled.nsls2.bnl.gov`), the mock `api.nsls2.bnl.gov` cycles server, the blackhole
and PVA IOCs, and the Redis/Tiled seeding are unchanged and still run as before. To roll one beamline back
to the previous per-service mechanisms (marketplace Redis 6 / Mongo actions, `docker run` Kafka, stunnel,
Tiled from the profile's pixi env), add `services-backend: actions` to its matrix `include` entry.
