from fastapi import FastAPI

app = FastAPI(title="spark-coach")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
