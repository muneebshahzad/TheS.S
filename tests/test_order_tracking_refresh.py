import main


def reset_refresh_state():
    with main.order_tracking_refresh_lock:
        main.order_tracking_refresh_state.update(
            running=False,
            error="",
            shopify_count=0,
            daraz_count=0,
            updated_at=0,
        )


def test_auto_refresh_defaults_to_one_hour_and_is_reported():
    assert main.TRACKING_AUTO_REFRESH_SECONDS == 3600
    state = main.get_order_tracking_refresh_state()
    assert state["auto_refresh_seconds"] == 3600


def test_background_refresh_worker_is_daemon(monkeypatch):
    reset_refresh_state()
    created = {}

    class DeferredThread:
        def __init__(self, *, target, daemon, name):
            created.update(target=target, daemon=daemon, name=name, started=False)

        def start(self):
            created["started"] = True

    monkeypatch.setattr(main.threading, "Thread", DeferredThread)
    assert main.start_order_tracking_refresh_background(thread_name="test-tracking-refresh") is True
    assert created["daemon"] is True
    assert created["started"] is True
    assert created["name"] == "test-tracking-refresh"
    reset_refresh_state()


def test_reload_keeps_live_cache_until_fetch_finishes_and_preserves_new_order(monkeypatch):
    old_order = {"id": 1, "order_id": "OLD", "created_at": "2026-09-01T00:00:00+00:00", "status": "In Transit"}
    refreshed_order = {"id": 1, "order_id": "OLD", "created_at": "2026-09-01T00:00:00+00:00", "status": "Delivered"}
    new_order = {"id": 2, "order_id": "NEW", "created_at": "2026-09-29T00:00:00+00:00", "status": "Un-Booked"}
    main.order_details = [old_order]
    monkeypatch.setattr(main, "setup_shopify", lambda: None)

    async def fetch_orders():
        assert main.order_details == [old_order]
        main.order_details.append(new_order)
        return [refreshed_order]

    monkeypatch.setattr(main, "get_shopify_orders", fetch_orders)
    assert main.reload_orders() is True
    assert {order["id"] for order in main.order_details} == {1, 2}
    assert next(order for order in main.order_details if order["id"] == 1)["status"] == "Delivered"


def test_order_removed_during_refresh_is_not_restored(monkeypatch):
    old_order = {"id": 1, "order_id": "OLD", "created_at": "2026-09-01T00:00:00+00:00"}
    main.order_details = [old_order]
    monkeypatch.setattr(main, "setup_shopify", lambda: None)

    async def fetch_orders():
        main.order_details = []
        return [dict(old_order)]

    monkeypatch.setattr(main, "get_shopify_orders", fetch_orders)
    assert main.reload_orders() is True
    assert main.order_details == []


def test_refresh_endpoint_starts_background_work_and_status_remains_available(monkeypatch):
    reset_refresh_state()
    monkeypatch.setattr(main, "start_order_tracking_refresh_background", lambda **_kwargs: True)
    client = main.app.test_client()
    response = client.post("/refresh", headers={"X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 202
    assert response.get_json()["message"] == "Tracking refresh started"
    status = client.get("/refresh/status")
    assert status.status_code == 200
    assert status.get_json()["auto_refresh_seconds"] == 3600

