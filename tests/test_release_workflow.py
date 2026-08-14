from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
PYPI_ACTION_SHA = "dc37677b2e1c63e2034f94d8a5b11f265b73ba33"


def test_release_workflow_uses_tagged_trusted_publishing_with_attestations() -> None:
    workflow = yaml.load(RELEASE_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert workflow["on"]["push"]["tags"] == ["v[0-9]*"]
    assert workflow["permissions"] == {"contents": "read"}

    build = workflow["jobs"]["build"]
    publish = workflow["jobs"]["publish-pypi"]
    assert build["permissions"] == {"contents": "read"}
    assert publish["needs"] == "build"
    assert publish["environment"] == {
        "name": "pypi",
        "url": "https://pypi.org/p/memory-lint",
    }
    assert publish["permissions"] == {"id-token": "write"}

    publish_step = publish["steps"][-1]
    assert publish_step["uses"] == f"pypa/gh-action-pypi-publish@{PYPI_ACTION_SHA}"
    assert publish_step["with"] == {"attestations": "true"}
