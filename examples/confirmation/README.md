# confirmation — tools that pause for the user's answer

A voice agent with four tools, chosen to cover the shapes a tool can have:

| tool | blocks on | shows |
|---|---|---|
| `look_and_tell` | camera consent | the run outliving the call — the answer it finally gives depends on the question asked *before* the wait |
| `record_meeting` | recording consent | a real refusal branch: no consent, no recording |
| `make_call` | a phone number, *only if the user did not say one* | blocking is a runtime fact, not a property of the tool — and the answer is a string, not a boolean |
| `get_date_and_time` | nothing | the same executor with no `ctx` at all |

Camera, microphone, vision and telephony are mocked (`mocks.py`). The subject here is
the round-trip, not device or network I/O.

## Run it

```
PYTHONPATH=examples python -m confirmation.demo
```

No API key, no audio, no network. The model's half is scripted so every branch —
including the ones you would otherwise have to wait for — happens on demand.

## What to look for in the output

The design is in `docs/claude/14-multi-step-tool-runs.md`; this is where you watch it
run.

**1. The ask closes the call.** `look_and_tell#c1` comes back `input_required`. As far
as Gemini is concerned that call is finished — it got a terminal response. The *run*
is not: it sits in the agent's slot holding the question.

**2. The answer's call carries the result.** The user says yes, the model calls
`provide_input`, and `look_and_tell`'s success payload comes back on **that** call
(`provide_input#c2`). One requirement, two calls. N requirements, N+1 calls.

**3. Nothing correlating crosses the model.** No run id, no ticket. `for_tool` and
`key` are semantic names the model can re-derive from what it just read; the actual
correlation is the connection the call arrived on, which names the agent, which has at
most one blocked run.

**4. The same tool takes both paths.** Scenarios 4 and 5: "call 98765 43210" dials
immediately — the model already has the value, so nothing blocks. "I want to make a
call" blocks on `phone_number`, and the answer comes back in `text_value` because the
requirement declares `expects: "string"`. One handler, one `await`, two behaviours.

**5. A topic change drops the old run silently.** Scenario 6: the user gives up on the
sign and asks for the date. The newer call takes the agent's slot; the blocked run is
cancelled with nothing sent, because its ask already closed the only call it had. When
a late consent finally arrives it is answered `skipped` — the model is not left
hanging, and is told to say nothing about it.

**6. A wrong answer never costs the user the right one.** Scenarios 7 and 8: an answer
for the wrong tool is `skipped`, an answer in the wrong slot is `invalid_args` and
retriable. Both leave the run blocked and still answerable.

**7. An unanswered question expires on its own budget.** Scenario 9:
`InputRequired.budget_s`, swept by `Session.sweep_runs`. Nothing is sent — there is no
open call to send it on.

## The pieces

| file | what it is |
|---|---|
| `tools.py` | the four tools + the registry, including `provide_input` built from the declared input keys |
| `agent.py` | the system instruction (task half + the framework's protocol block) and the Gemini Live `AgentSpec` |
| `mocks.py` | camera / microphone / telephony / clock stand-ins |
| `demo.py` | the scripted walkthrough |

`tests/test_example_confirmation.py` runs all of it headless.

## Going live

`agent.py:build_agent_spec()` is a real Gemini Live spec — same shape as
`examples/multi-agent/backend/agents.py`. Point a bridge at it and the flow is
unchanged, because none of the machinery above is vendor-specific: the only thing the
vendor sees is ordinary function calls and ordinary function responses.

Not yet verified against the live API (docs 14, O5): whether Gemini Live honours the
`enum` on `provide_input.key`, and how reliably the model fills the slot named by
`expects`. The framework validates both regardless — that is what the `invalid_args`
and mismatch paths above are for.
