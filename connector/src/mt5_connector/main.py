import argparse, json
from .config import ConnectorConfig
from .adapter import OfficialMT5Adapter
def main(argv=None):
 p=argparse.ArgumentParser(); p.add_argument("--config",required=True); p.add_argument("--initialize",action="store_true"); a=p.parse_args(argv)
 cfg=ConnectorConfig.from_json(open(a.config,encoding="utf-8").read()); print(json.dumps({"account_id":cfg.account_id,"execution_disabled":cfg.execution_disabled}))
 if a.initialize: OfficialMT5Adapter(__import__("mt5_connector.models",fromlist=["Identity"]).Identity(cfg.provider,cfg.broker_server,cfg.external_account_id),cfg.account_id).initialize(cfg.terminal_path)
if __name__=="__main__": main()
