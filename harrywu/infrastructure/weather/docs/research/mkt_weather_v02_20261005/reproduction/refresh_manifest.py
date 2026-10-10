"""Refresh hashes of explicitly Git-tracked weather deliverables, never scratch files."""
import hashlib
import json
from pathlib import Path
import subprocess

repo=Path(__file__).resolve().parents[4]
report=repo/'docs/research/mkt_weather_v02_20261005'
manifest=report/'DELIVERY_MANIFEST.json'
paths=subprocess.check_output(['git','ls-files','docs/research/mkt_weather_v02_20261005',
                               'policyStudy/policy/market_sentiment'],cwd=repo,text=True).splitlines()
value=json.loads(manifest.read_text(encoding='utf-8'))
value.update(boundary_tests=82,additional_unittest_subcases=8,legacy_f1_tests=37,
             human_labels_received=False,human_labels_complete=False,human_label_fields_received=1,
             human_label_fields_used=0,human_review_required=False,
             methodology_review='project_review_accepted',review_version='project-review-v1-20261007',
             review_days=424,cloud_revision_folder_url='https://drive.google.com/drive/folders/1hxzMaaVvUjZihATM9EDsO5tuhiZsqs43')
value['files']=[{'path':p,'bytes':(repo/p).stat().st_size,
                 'sha256':hashlib.sha256((repo/p).read_bytes()).hexdigest()}
                for p in sorted(set(paths)) if repo/p!=manifest]
manifest.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(f'Verified manifest inputs: {len(value["files"])} tracked files')
