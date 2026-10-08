"""Conservative first-pass guidance for an idle native request.

This is a small rule set, not a substitute for reading the user's request.
Unknown scope remains inspectable and can be escalated with native begin.
"""
from __future__ import annotations

import re


_APPROVAL = re.compile(r"^(?:(?:yes|yep|okay|ok|네|응|ㅇㅇ|좋아)\s+)?(?:do it|go ahead|proceed|그렇게 해줘|진행해줘|해줘)[.!。\s]*$|^(?:yes|yep|okay|ok|네|응|ㅇㅇ|좋아)[.!。\s]*$", re.I)
_GREETING = re.compile(r"^(?:hi|hello|hey|안녕|안녕하세요)[.!。\s]*$", re.I)
_STATUS = re.compile(r"^(?:status|progress|what(?:'s| is) the status|how(?:'s| is) it going|"
                     r"(?:현재\s*)?진행\s*상황(?:을?\s*알려줘|이?\s*어때)?|"
                     r"상태(?:를?\s*알려줘|가?\s*어때)?|(?:지금\s*)?어디까지\s*(?:했어|됐어)|지금 상태)[?!.。\s]*$", re.I)
_EXPLANATION = re.compile(r"^(?:what(?:'s| is| are)|why|how does|define|explain|뜻(?:이 뭐야|은|이야)?|무엇|뭐야|왜|설명(?:해줘|해)?|알려줘)", re.I)
_REPO_REFERENCE = re.compile(r"(?:\b(?:our|current|repo|repository|project|function|file|code|log|deployment|login|auth|database)\b|이\s*(?:함수|파일|코드|프로젝트|레포|로그)|우리\s*(?:코드|프로젝트)|로그인|배포\s*상태)", re.I)
_TYPO = re.compile(r"(?:typo|spelling|오타|맞춤법)", re.I)
_ISOLATED = re.compile(r"(?:\b(?:this|one|single|following)\s+(?:word|sentence|line|paragraph)\b|이\s*(?:단어|문장|한\s*줄)|다음\s*(?:단어|문장)|['\"“”‘’][^\n]{1,120}['\"“”‘’])", re.I)
_SENSITIVE = re.compile(r"(?:\b(?:database|db|auth|login|permission|credential|secret|finance|payment|bank|delete|remove|deploy|publish|production|security)\b|데이터베이스|디비|인증|로그인|권한|비밀번호|결제|금융|은행|삭제|지워|배포|게시|운영\s*서버|보안)", re.I)
_ACTION = re.compile(r"(?:\b(?:fix|build|implement|install|create|change|modify|update|deploy|publish|delete|remove|refactor|migrate|run|test|review|investigate|debug)\b|고쳐|수정|구현|설치|만들|변경|업데이트|배포|게시|삭제|지워|리팩터|마이그레이션|실행|테스트|검토|조사|분석)", re.I)
_DEEP = re.compile(r"(?:\b(?:architecture|migration|research|production|end.to.end|security audit)\b|아키텍처|마이그레이션|연구|운영\s*배포|전체\s*시스템|종합\s*보고서)", re.I)
_BOUNDED = re.compile(r"(?:\b(?:this|single|one)\s+(?:file|function|test)\b|이\s*(?:파일|함수|테스트)|한\s*(?:파일|함수|테스트))", re.I)
_SMALL_ACTION = re.compile(r"(?:\b(?:fix|correct|rename|adjust)\b|오타\s*수정|이름\s*바꿔|작은\s*수정)", re.I)
_FOLLOW_ON_ACTION = re.compile(r"(?:\b(?:then|and)\s+(?:implement|fix|save|write|export|create|build|publish|deploy|delete|remove)\b|(?:하고|해서|한\s*뒤|후)\s*(?:구현|수정|저장|작성|내보내|생성|배포|삭제)|PDF\s*(?:로|파일)|파일\s*(?:로|에)\s*저장)", re.I)
_TRANSLATION = re.compile(r"^(?:translate\b|번역(?:해줘|해| 부탁해|\s*:)?)", re.I)
_QUOTED = re.compile(r'''"[^"\n]{0,500}"|'[^'\n]{0,500}'|“[^”\n]{0,500}”|‘[^’\n]{0,500}’''')
_TRANSLATION_REFERENCE = re.compile(
    r"(?:\b(?:this|that|the|our|current|attached|uploaded)\s+(?:file|document|report|page|pdf|repo|repository|project|code|log|function)\b"
    r"|\battachment\b"
    r"|(?:이|그|첨부(?:한|된)?|현재|위)\s*(?:파일|문서|보고서|PDF|프로젝트|코드|로그|함수)"
    r"|(?:^|\s)@[^\s]+)", re.I)
_TRANSLATION_FILE = re.compile(
    r"(?:\b[^\s/\\:]+\.[A-Za-z][A-Za-z0-9]{0,11}\b"
    r"|\b[A-Za-z]:[\\/][^\s]+|\\\\[^\s]+"
    r"|(?:^|\s)(?:\.{1,2}[\\/]|~[\\/]|[\\/])[^\s]+"
    r"|(?:^|\s)[\w.-]+(?:[\\/][\w.-]+)+)", re.I)
_TRANSLATION_PRONOUN = re.compile(
    r"(?:it|this|that|above|attached|the above|the attached|the attachment|이것|그것|위의 것|첨부(?:한|된)? 것)"
    r"(?:\s+(?:to|into|in)\s+[\w-]+|\s*(?:한국어|영어)로)?", re.I)
_WIDE_SCOPE = re.compile(r"(?:\b(?:all|every|entire|throughout|project.wide|repo.wide)\b|전체|모든|프로젝트|레포|코드베이스)", re.I)


def is_brief_approval(prompt: str) -> bool:
    return bool(_APPROVAL.fullmatch(prompt.strip()))


def is_status_followup(prompt: str) -> bool:
    """Standalone progress questions can refer to an accepted run."""
    return bool(_STATUS.fullmatch(prompt.strip()))


def classify_request(prompt: str, *, previous: str | None = None,
                     active_contract: bool = False) -> dict[str, str]:
    """Return an explainable path; only narrow forms may skip native begin."""
    text = prompt.strip()
    if active_contract:
        return {'path': 'active', 'reason': 'Existing contract needs native intent reconciliation.',
                'suggested_profile': 'standard', 'effective_prompt': text}
    if is_brief_approval(text):
        if previous and is_substantive_request_source(previous):
            prior = classify_request(previous)
            return {**prior, 'reason': 'Brief approval inherits the preceding substantive request: ' + prior['reason'],
                    'effective_prompt': previous.strip()}
        return {'path': 'inspect', 'reason': 'Brief approval has no substantive request in this session.',
                'suggested_profile': 'standard', 'effective_prompt': text}
    if not text:
        return {'path': 'inspect', 'reason': 'Empty request needs context.',
                'suggested_profile': 'standard', 'effective_prompt': text}
    if _GREETING.fullmatch(text) or _STATUS.fullmatch(text):
        return {'path': 'direct', 'reason': 'Standalone greeting or status question.',
                'suggested_profile': 'simple', 'effective_prompt': text}
    # The requested operation matters more than verbs inside quoted source text.
    translation = _TRANSLATION.match(text)
    if translation:
        operand = text[translation.end():].strip().lstrip(':').strip()
        instruction = _QUOTED.sub('', text)
        if not operand.strip(' :\'"“”‘’') or re.fullmatch(r'(?:to|into|in)\s+\w+|(?:한국어|영어)로', operand, re.I):
            return {'path': 'inspect', 'reason': 'Translation text was not supplied.',
                    'suggested_profile': 'standard', 'effective_prompt': text}
        if _FOLLOW_ON_ACTION.search(instruction):
            return {'path': 'inspect', 'reason': 'Translation also requests another action or output artifact.',
                    'suggested_profile': 'standard', 'effective_prompt': text}
        unquoted = _QUOTED.sub('', operand).strip()
        if (_TRANSLATION_REFERENCE.search(unquoted) or _TRANSLATION_FILE.search(unquoted)
                or _TRANSLATION_PRONOUN.fullmatch(unquoted)):
            return {'path': 'inspect', 'reason': 'Translation input needs project or file inspection.',
                    'suggested_profile': 'standard', 'effective_prompt': text}
        return {'path': 'direct', 'reason': 'Self-contained translation request.',
                'suggested_profile': 'simple', 'effective_prompt': text}
    # Read-only wording governs quoted action words. Repository-dependent
    # explanations still need inspection before answering.
    explanation = (bool(_EXPLANATION.match(text)) or
                   bool(re.search(r'(?:뭐야|무엇인가|무슨 뜻(?:이야)?|어떤 의미(?:야)?)[?？]?$', text)))
    if explanation:
        if _FOLLOW_ON_ACTION.search(text):
            return {'path': 'inspect', 'reason': 'Explanation includes an additional action.',
                    'suggested_profile': 'standard', 'effective_prompt': text}
        if _REPO_REFERENCE.search(text):
            return {'path': 'inspect', 'reason': 'Answer depends on current project context.',
                    'suggested_profile': 'standard', 'effective_prompt': text}
        return {'path': 'direct', 'reason': 'Self-contained explanation or translation.',
                'suggested_profile': 'simple', 'effective_prompt': text}
    operation=_QUOTED.sub('', text)
    if (_TYPO.search(operation) and _ISOLATED.search(text)
            and not _FOLLOW_ON_ACTION.search(operation) and not _WIDE_SCOPE.search(operation)):
        return {'path': 'direct', 'reason': 'Explicitly isolated text correction.',
                'suggested_profile': 'simple', 'effective_prompt': text}
    if _DEEP.search(text):
        return {'path': 'deep', 'reason': 'Substantial architecture, migration, research or production scope.',
                'suggested_profile': 'deep', 'effective_prompt': text}
    if _SENSITIVE.search(text):
        return {'path': 'inspect', 'reason': 'Sensitive or potentially consequential action needs scope inspection.',
                'suggested_profile': 'standard', 'effective_prompt': text}
    if _ACTION.search(text):
        if _BOUNDED.search(text) and _SMALL_ACTION.search(text):
            return {'path': 'simple', 'reason': 'Action is explicitly bounded to one local unit.',
                    'suggested_profile': 'simple', 'effective_prompt': text}
        return {'path': 'inspect', 'reason': 'Action scope is not established by the request alone.',
                'suggested_profile': 'standard', 'effective_prompt': text}
    return {'path': 'inspect', 'reason': 'Request semantics or scope need a brief inspection.',
            'suggested_profile': 'standard', 'effective_prompt': text}


def is_substantive_request_source(prompt: str) -> bool:
    """Ignore small social/status turns when resolving a short approval."""
    text=prompt.strip()
    return bool(text and not is_brief_approval(text) and
                classify_request(text)['path']!='direct')
