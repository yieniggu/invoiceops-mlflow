"""The real MLflow client, configured only with a tracking URI and a token."""

from __future__ import annotations

import pytest
from conftest import WS_B


def test_log_model_and_register_model_with_only_uri_and_token(
    gateway: str, tokens: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import mlflow  # noqa: PLC0415
    from mlflow.pyfunc import PythonModel  # noqa: PLC0415

    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "MLFLOW_WORKSPACE",
        "MLFLOW_S3_ENDPOINT_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", gateway)
    monkeypatch.setenv("MLFLOW_TRACKING_TOKEN", tokens["b_write"])
    monkeypatch.setenv("GIT_PYTHON_REFRESH", "quiet")
    monkeypatch.setenv("MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR", "false")

    class Echo(PythonModel):
        def predict(self, context, model_input, params=None):  # type: ignore[no-untyped-def]
            return model_input

    mlflow.set_tracking_uri(gateway)
    experiment = mlflow.set_experiment("e2e-client")
    assert experiment.workspace == WS_B  # the gateway, not the client, chose the workspace
    with mlflow.start_run() as run:
        mlflow.log_param("p", 1)
        mlflow.log_metric("auc", 0.9)
        info = mlflow.pyfunc.log_model(name="model", python_model=Echo(), pip_requirements=[])
        version = mlflow.register_model(info.model_uri, "e2e-model")
    assert version.version == "1"
    client = mlflow.MlflowClient()
    assert client.get_registered_model("e2e-model").name == "e2e-model"
    mlflow.artifacts.download_artifacts(artifact_uri="models:/e2e-model/1", dst_path=str(tmp_path))
    assert (tmp_path / "MLmodel").exists()
    loaded = mlflow.pyfunc.load_model(f"models:/e2e-model/{version.version}")
    assert loaded is not None

    # A token of another organization cannot see any of it.
    monkeypatch.setenv("MLFLOW_TRACKING_TOKEN", tokens["a_write"])
    other = mlflow.MlflowClient()
    try:
        visible = [e.name for e in other.search_experiments()]
    except mlflow.exceptions.MlflowException:
        visible = []  # reads never provision: an untouched workspace answers 404
    assert "e2e-client" not in visible
    with pytest.raises(mlflow.exceptions.MlflowException):
        other.get_run(run.info.run_id)
