# RynMesh public website

This directory is the canonical public source for `rynmesh.ai`. It is a static,
progressively enhanced site: product and contributor documentation render
without JavaScript, while release and accepted-work status are read from the
public GitHub API at runtime.

Run it locally from the repository root:

```bash
python3 -m http.server 4173 --directory website
```

Then open `http://127.0.0.1:4173`. Do not add GitHub tokens, private deployment
configuration, invented contributor identities, or private infrastructure to
this directory. GitHub Issues, pull requests, releases, and Actions remain the
source of truth for coordination and package status.
