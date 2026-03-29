use axum::{
    Router,
    extract::State,
    http::header,
    response::{IntoResponse, Json},
    routing::get,
};
use serde_json::{Value, json};

use crate::state::AppState;

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/health", get(health))
        .route("/info", get(info))
        .route("/interfaces", get(interfaces))
}

pub fn observability_routes() -> Router<AppState> {
    Router::new().route("/metrics", get(prometheus_metrics))
}

async fn prometheus_metrics(State(state): State<AppState>) -> impl IntoResponse {
    let body = state.metrics().to_prometheus();
    (
        [(
            header::CONTENT_TYPE,
            "text/plain; version=0.0.4; charset=utf-8",
        )],
        body,
    )
}

async fn health() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

async fn interfaces(State(state): State<AppState>) -> Json<Value> {
    let entries = if let Some(session) = state.session_store() {
        let names = session.lock().await.get_interface_names();
        let mut entries: Vec<Value> = names
            .into_iter()
            .map(|((exporter, domain_id, index), name)| {
                json!({
                    "exporter": exporter.to_string(),
                    "domain_id": domain_id,
                    "index": index,
                    "name": name,
                })
            })
            .collect();
        entries.sort_by(|a, b| {
            let key = |v: &Value| {
                (
                    v["exporter"].as_str().unwrap_or("").to_string(),
                    v["domain_id"].as_u64().unwrap_or(0),
                    v["index"].as_u64().unwrap_or(0),
                )
            };
            key(a).cmp(&key(b))
        });
        entries
    } else {
        Vec::new()
    };

    Json(json!({ "interfaces": entries }))
}

async fn info(State(state): State<AppState>) -> Json<Value> {
    let config = state.config();
    Json(json!({
        "name": "flowcus",
        "version": env!("CARGO_PKG_VERSION"),
        "server": {
            "host": config.server.host,
            "port": config.server.port,
            "dev_mode": config.server.dev_mode,
        },
        "storage": {
            "merge_workers": config.storage.merge_workers,
        }
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    use flowcus_core::AppConfig;

    fn test_state() -> AppState {
        let config = AppConfig::default();
        let metrics = flowcus_core::observability::Metrics::new();
        AppState::new(config, metrics)
    }

    #[tokio::test]
    async fn health_returns_ok() {
        let app = routes().with_state(test_state());
        let req = Request::builder()
            .uri("/health")
            .body(Body::empty())
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn info_returns_version() {
        let app = routes().with_state(test_state());
        let req = Request::builder().uri("/info").body(Body::empty()).unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }
}
