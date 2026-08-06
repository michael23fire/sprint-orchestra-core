from app.config import Settings
from app.main import _workflow_checkpoint_db_url


def test_sprint_recovery_only_uses_its_own_checkpoint_dsn():
    settings = Settings(
        epic_rollout_enabled=False,
        sprint_recovery_enabled=True,
        epic_rollout_checkpoint_db_url="postgresql://ignored",
        sprint_recovery_checkpoint_db_url="postgresql://sprint",
    )

    assert _workflow_checkpoint_db_url(settings) == "postgresql://sprint"


def test_epic_rollout_only_uses_its_own_checkpoint_dsn():
    settings = Settings(
        epic_rollout_enabled=True,
        sprint_recovery_enabled=False,
        epic_rollout_checkpoint_db_url="postgresql://epic",
        sprint_recovery_checkpoint_db_url="postgresql://ignored",
    )

    assert _workflow_checkpoint_db_url(settings) == "postgresql://epic"


def test_both_workflows_reject_different_checkpoint_databases():
    settings = Settings(
        epic_rollout_enabled=True,
        sprint_recovery_enabled=True,
        epic_rollout_checkpoint_db_url="postgresql://epic",
        sprint_recovery_checkpoint_db_url="postgresql://sprint",
    )

    try:
        _workflow_checkpoint_db_url(settings)
    except ValueError as exc:
        assert "must match" in str(exc)
    else:
        raise AssertionError("different checkpoint DSNs must not be silently accepted")


def test_no_durable_workflow_needs_no_checkpoint_database():
    settings = Settings(epic_rollout_enabled=False, sprint_recovery_enabled=False)

    assert _workflow_checkpoint_db_url(settings) is None
