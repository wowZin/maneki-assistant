#!/usr/bin/env python3
"""LLM 盲测：判断「真拉升(买)」vs「假拉升(不买)」的准确率。

对 80 个带标签样本（各40），只给文本描述（不含标签），调 deepseek 判断「买/不买」，
对比真实标签算：整体准确率 / 真拉升召回率 / 假拉升正确率 / 混淆矩阵。

基线 = 随机 50%。若 LLM 显著 > 50%，说明它有区分能力。
"""
import json, yaml, requests, time, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, '/root/maneki-agent')

SAMPLE_FILE = '/root/maneki-agent/wiki/raw/cb-play/llm_test_samples.json'

cfg = yaml.safe_load(open('/root/.hermes/config.yaml'))
m = cfg['model']
URL = m['base_url'].rstrip('/') + '/chat/completions'
HEADERS = {'Authorization': f"Bearer {m['api_key']}", 'Content-Type': 'application/json'}
MODEL = m['default']

SYSTEM = ('你是A股短线交易助手，专注追涨打板。根据给出的盘中分钟数据，'
          '判断当前时点值不值得追涨买入。你只看数据本身，不做其他假设。'
          '严格只回答一个词：「买」或「不买」。')


def ask(text):
    payload = {
        'model': MODEL,
        'messages': [
            {'role': 'system', 'content': SYSTEM},
            {'role': 'user', 'content': text},
        ],
        'temperature': 0,
        'max_tokens': 400,
    }
    try:
        r = requests.post(URL, json=payload, headers=HEADERS, timeout=40)
        r.raise_for_status()
        content = r.json()['choices'][0]['message']['content'].strip()
        return content
    except Exception as e:
        return f'__ERROR__ {str(e)[:80]}'


def parse_verdict(content):
    """解析 LLM 输出为 buy/skip。"""
    c = content.strip()
    if '不买' in c or '不追' in c or c.startswith('skip') or c.startswith('Skip'):
        return 'skip'
    if '买' in c or c.startswith('buy') or c.startswith('Buy'):
        return 'buy'
    return 'unclear'


def main():
    samples = json.load(open(SAMPLE_FILE))
    print(f'盲测样本 {len(samples)} 个（真拉升 buy / 假拉升 skip 各 40）')

    # 先测 1 个确认 API 通
    print('\n测试 API 连通性...')
    test_resp = ask(samples[0]['text'])
    print(f'样例 LLM 输出: {test_resp[:80]}')
    if test_resp.startswith('__ERROR__'):
        print('API 调用失败，检查 key/model')
        sys.exit(1)

    # 批量并发
    print(f'\n批量盲测 {len(samples)} 个（并发 6）...')
    results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(ask, s['text']): i for i, s in enumerate(samples)}
        done = 0
        for fut in as_completed(futures):
            i = futures[fut]
            results[i] = fut.result()
            done += 1
            if done % 20 == 0:
                print(f'  进度 {done}/{len(samples)}')

    # 统计
    tp = tn = fp = fn = 0
    unclear = 0
    detail = []
    for i, s in enumerate(samples):
        verdict = parse_verdict(results.get(i, ''))
        truth = s['label']  # buy=真拉升, skip=假拉升
        if verdict == 'unclear':
            unclear += 1
            detail.append((s['code'], truth, 'unclear', results.get(i, '')[:40]))
            continue
        pred_buy = (verdict == 'buy')
        actual_buy = (truth == 'buy')
        if pred_buy and actual_buy:
            tp += 1
        elif pred_buy and not actual_buy:
            fp += 1
        elif not pred_buy and not actual_buy:
            tn += 1
        else:
            fn += 1
        detail.append((s['code'], truth, verdict, ''))

    n = tp + tn + fp + fn
    acc = (tp + tn) / n * 100 if n else 0
    # 真拉升召回率 = tp / (tp+fn)
    recall = tp / (tp + fn) * 100 if (tp + fn) else 0
    # 假拉升正确率 = tn / (tn+fp)
    specificity = tn / (tn + fp) * 100 if (tn + fp) else 0
    # 精确率 = tp / (tp+fp)
    precision = tp / (tp + fp) * 100 if (tp + fp) else 0

    print('\n' + '=' * 60)
    print('LLM 盲测结果（基线=随机 50%）')
    print('=' * 60)
    print(f'有效样本 {n} 个（无法解析 {unclear} 个）')
    print(f'\n混淆矩阵（行=LLM判断, 列=真实）:')
    print(f'            真拉升   假拉升')
    print(f'  判断"买"   {tp:<6}   {fp:<6}   (精确率 {precision:.0f}%)')
    print(f'  判断"不买" {fn:<6}   {tn:<6}   (假拉升正确率 {specificity:.0f}%)')
    print(f'\n整体准确率: {acc:.1f}%')
    print(f'真拉升召回率(该买时买了): {recall:.1f}%')
    print(f'假拉升正确率(该躲时躲了): {specificity:.1f}%')

    # 关键：如果 LLM 判断"买"的票，实际胜率
    if tp + fp > 0:
        print(f'\nLLM 说"买"的 {tp+fp} 只里，真涨 {tp} 只（命中率 {precision:.0f}%）')
    if tn + fn > 0:
        print(f'LLM 说"不买"的 {tn+fn} 只里，真跌 {tn} 只（{specificity:.0f}%）')

    # 输出无法解析的
    if unclear:
        print(f'\n无法解析 {unclear} 个:')
        for code, truth, v, raw in detail:
            if v == 'unclear':
                print(f'  {code} {truth} → {raw}')


if __name__ == '__main__':
    main()
