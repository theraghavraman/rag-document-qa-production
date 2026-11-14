"""Run a golden-set retrieval/citation evaluation against an actual deployed API."""
import argparse
import json
import os
import statistics
import sys
import time
import httpx


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dataset', help='JSONL: question, expected_keywords, document_ids (optional), should_abstain (optional)')
    ap.add_argument('--url', default='http://localhost:8000')
    ap.add_argument('--min-pass-rate', type=float, default=0.9)
    args = ap.parse_args()
    key = os.environ.get('RAG_API_KEY')
    if not key:
        ap.error('Set RAG_API_KEY in environment')
    with open(args.dataset, encoding='utf-8') as f:
        examples = [json.loads(x) for x in f if x.strip()]
    results = []
    latencies = []
    with httpx.Client(timeout=120) as client:
        for entry in examples:
            started = time.perf_counter()
            response = client.post(args.url + '/v1/qa/query', headers={'X-API-Key': key},
                                   json={'question': entry['question'],
                                         'document_ids': entry.get('document_ids')})
            response.raise_for_status()
            elapsed = round(time.perf_counter() - started, 3)
            latencies.append(elapsed)
            result = response.json()
            answer = result['answer'].lower()
            should_abstain = bool(entry.get('should_abstain', False))
            if should_abstain:
                valid = not result['grounded']
            else:
                valid = bool(result.get('citations_valid', True) and result['grounded'])
            keywords = all(word.lower() in answer for word in entry.get('expected_keywords', []))
            passed = valid and keywords
            results.append({
                'question': entry['question'],
                'passed': passed,
                'should_abstain': should_abstain,
                'grounded': result['grounded'],
                'citations_valid': result.get('citations_valid', result['grounded']),
                'latency_seconds': elapsed,
            })
            print(json.dumps(results[-1]))
    sorted_lat = sorted(latencies)
    fraction = sum(int(x['passed']) for x in results) / max(1, len(results))
    summary = {
        'total_examples': len(results),
        'pass_rate': round(fraction, 4),
        'p50_latency_seconds': _percentile(sorted_lat, 50),
        'p95_latency_seconds': _percentile(sorted_lat, 95),
        'mean_latency_seconds': round(statistics.mean(latencies), 3) if latencies else 0.0,
    }
    print(json.dumps({'summary': summary}))
    if fraction < args.min_pass_rate:
        sys.exit(1)


if __name__ == '__main__':
    main()
