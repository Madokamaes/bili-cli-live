"""Run bili with guarded compatibility fixes and HTTPX transport."""
from bilibili_api import select_client
select_client("httpx")
from coin_form_patch import apply as apply_coin_form_patch
apply_coin_form_patch()
from response_diagnostics_patch import apply as apply_response_diagnostics_patch
apply_response_diagnostics_patch()
from bili_cli.main import main, app
from live_cli import register as register_live
register_live(app)
if __name__ == "__main__":
    main()
