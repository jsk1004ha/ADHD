# Initial request routing

An idle native hook records an initial route in `bridge/.../view.json` and emits brief guidance when the route changes. Use `adhd.py request-route "REQUEST"` to inspect the same rule set without starting a run. It is conservative guidance, not a calibrated confidence score or complete language understanding.

| Route | Example | Next action |
| --- | --- | --- |
| `direct` | `배포가 뭐야?`, `Translate: fix login`, a standalone status request | Answer with appropriate checking. No native begin, durable plan, or independent acceptance loop. |
| `simple` | `Fix this function` | Begin a bounded native run if work is needed. Plan optional; independent acceptance remains required. |
| `inspect` | `로그인 고쳐줘`, `이 함수 왜 느려?`, `번역하고 PDF로 저장해줘` | Inspect the actual request and relevant context briefly, then begin with an appropriate profile if substantive. |
| `deep` | Architecture migration or broad research | Begin with the deep profile unless the user chose another profile. |

An explicit `native begin` `execution_profile` or CLI `--profile` overrides the suggestion; routing does not change the selected model or reasoning effort. The idle route never substitutes for authorization, required validation, or project instructions. A direct request can escalate through a real native begin if its scope grows. Short approvals such as `ㅇㅇ 그렇게 해줘` inherit the preceding substantive request instead of losing it. While a contract is active, new turns remain pending until `native sync-intent` classifies them; stop and resume handling retain the original contract. A new standalone direct question after completed work does not reopen the accepted contract.
