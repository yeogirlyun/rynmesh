"""A job needs a brief only when nothing else says what to make."""


import pytest

import test_image_studio  # noqa: F401  (puts the plugin on the import path)
from ryn_image_studio.jobs import JobError, JobStore, compose_brief


def test_brief_is_optional_with_copy_or_images(tmp_path):
    store = JobStore(tmp_path)
    try:
        job = store.submit("c", "n", "p", {"scene": "banner", "copy": {"title": "冬季新品"}})[0]
        assert job["brief"] == "" and job["copy"] == {"title": "冬季新品"}
        assert "冬季新品" in compose_brief(store.job(None, job["id"]))
        for empty in ({"scene": "banner"}, {"purpose": "main", "brief": "  "}, {"scene": "banner", "copy": {"title": ""}}):
            with pytest.raises(JobError, match="image_bad_brief"):
                store.submit("c", "n", "p", empty)
    finally:
        store.close()
