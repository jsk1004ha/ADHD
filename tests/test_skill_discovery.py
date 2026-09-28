from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from adhd.core import discover_skills
from adhd.skill_router import configure_router, route_skills


def make_skill(root: Path, folder: str, name: str='same-name') -> Path:
    path=root/folder/'SKILL.md';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: parser regression checks\n---\n',encoding='utf-8')
    return path


class SkillDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.repo=self.base/'repo';self.repo.mkdir();(self.repo/'.git').mkdir();self.ws=self.repo/'a'/'b';self.ws.mkdir(parents=True)
        self.codex=self.base/'codex';self.codex.mkdir();self.env=patch.dict(os.environ,{'CODEX_HOME':str(self.codex),'ADHD_HOME':str(self.base/'state')})
        self.env.start()

    def tearDown(self): self.env.stop();self.tmp.cleanup()

    def test_project_and_ancestor_skills_found(self):
        skill=make_skill(self.repo/'.agents'/'skills','ancestor','ancestor-skill')
        found=discover_skills(self.ws)
        self.assertIn(str(skill.resolve()),[x['path'] for x in found])

    def test_disabled_skill_is_excluded(self):
        skill=make_skill(self.codex/'skills','disabled','disabled-skill')
        (self.codex/'config.toml').write_text('[[skills.config]]\npath='+json.dumps(str(skill))+'\nenabled=false\n')
        self.assertNotIn(str(skill.resolve()),[x['path'] for x in discover_skills(self.ws)])

    def test_same_name_different_paths_are_preserved(self):
        one=make_skill(self.repo/'.agents'/'skills','one');two=make_skill(self.codex/'skills','two')
        paths=[x['path'] for x in discover_skills(self.ws) if x['name']=='same-name']
        self.assertEqual(set(paths),{str(one.resolve()),str(two.resolve())})

    def test_host_catalog_filters_disabled_entries(self):
        one=make_skill(self.repo,'catalog-one');two=make_skill(self.repo,'catalog-two')
        rows=discover_skills(self.ws,host_catalog=[{'path':str(one),'name':'one','enabled':True},{'path':str(two),'name':'two','enabled':False}])
        self.assertEqual([x['name'] for x in rows],['one'])

    def test_stale_router_requests_bounded_refresh_and_reports_failure(self):
        script=self.base/'skill_wiki.py';script.write_text('print(1)');configure_router(script)
        payload={'status':'stale','warnings':['refresh_failed:plugin-discovery:timeout']}
        completed=subprocess.CompletedProcess([],3,stdout=json.dumps(payload),stderr='')
        with patch('adhd.skill_router.subprocess.run',return_value=completed) as run:
            result=route_skills('parser',workspace=self.ws)
        self.assertEqual(result['status'],'stale');self.assertEqual(run.call_count,1)
        self.assertEqual(run.call_args.args[0].count('--refresh-stale'),1)
        self.assertEqual(run.call_args.kwargs['timeout'],90)
        self.assertEqual(run.call_args.kwargs['cwd'],self.ws.resolve())

    def test_refreshed_router_result_is_used_without_fallback(self):
        script=self.base/'skill_wiki.py';script.write_text('print(1)');configure_router(script)
        payload={'status':'ok','warnings':['artifacts_refreshed'],'read_order':[{'name':'test-skill'}]}
        completed=subprocess.CompletedProcess([],0,stdout=json.dumps(payload),stderr='')
        with patch('adhd.skill_router.subprocess.run',return_value=completed) as run:
            result=route_skills('parser',workspace=self.ws)
        self.assertEqual(result['engine'],'existing-wiki-route-v7')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['result'],payload)
        self.assertEqual(run.call_count,1)

    def test_valid_no_match_is_preserved(self):
        script=self.base/'skill_wiki.py';script.write_text('print(1)');configure_router(script)
        completed=subprocess.CompletedProcess([],0,stdout=json.dumps({'status':'no_match'}),stderr='')
        with patch('adhd.skill_router.subprocess.run',return_value=completed):
            result=route_skills('uncovered',workspace=self.ws)
        self.assertEqual(result['engine'],'existing-wiki-route-v7')
        self.assertEqual(result['status'],'no_match')

    def test_outer_router_timeout_is_visible(self):
        script=self.base/'skill_wiki.py';script.write_text('print(1)');configure_router(script)
        with patch('adhd.skill_router.subprocess.run',side_effect=subprocess.TimeoutExpired(['wiki'],90)) as run:
            result=route_skills('parser',workspace=self.ws)
        self.assertEqual(run.call_count,1)
        self.assertEqual(result['engine'],'local-metadata-fallback')
        self.assertTrue(any('Existing router unavailable' in warning for warning in result['warnings']))


if __name__=='__main__': unittest.main()
