"""The web app: API for the UI and the phone, and the place the jobs start from."""

from fastapi import FastAPI

from agentc import __version__

app = FastAPI(title="AgentC Edge", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check for the hub's service manager and CI."""
    return {"status": "ok", "version": __version__}
