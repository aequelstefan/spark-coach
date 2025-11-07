from fastapi import FastAPI

from spark_coach.api.v1.slack import router as slack_router

app = FastAPI(title="spark-coach")

# Routers
app.include_router(slack_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
