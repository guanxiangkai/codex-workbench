#!/usr/bin/env python3
"""由会话调用方登记已核对的模型终态；不执行网络请求，不读取 Key。"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from codex_workbench.model_catalog import configured_models
from codex_workbench.model_observations import ModelObservations, configuration_fingerprint


def main():
    """调用前获取配置指纹，调用后携同指纹记录，防止配置变更导致误关联。"""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resources-dir',type=Path,required=True)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--model-id',required=True)
    parser.add_argument('--outcome',choices=('verified','failed'))
    parser.add_argument('--fingerprint')
    parser.add_argument('--observed-at',help='真实调用时间，ISO 8601，必须包含时区')
    args=parser.parse_args()
    model=next((m for m in configured_models(args.resources_dir/'models/catalog.json') if m['id']==args.model_id),None)
    if model is None:parser.error('目录中不存在此模型')
    fingerprint=configuration_fingerprint(model)
    if not args.outcome:
        print(fingerprint);return
    if args.fingerprint!=fingerprint:parser.error('调用配置指纹不匹配；请核对实际调用时使用的配置')
    ModelObservations(args.data_dir).record(model,success=args.outcome=='verified',source='conversation',observed_at=args.observed_at)
    print('模型调用结果已记录')


if __name__=='__main__':main()
