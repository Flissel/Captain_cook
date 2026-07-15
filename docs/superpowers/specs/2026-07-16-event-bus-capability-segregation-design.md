# Event Bus Capability Segregation Design

## Problem

`agenten.runtime.event_bus.EventBus` currently requires both `publish()` and
callable `subscribe()`. `InMemoryEventBus` supports both operations, while
AutoGen Core routes topic types to registered agent types through
`TypeSubscription` and cannot faithfully attach arbitrary callables. As a
result, `AutoGenEventBus` nominally implements `EventBus` but raises
`NotImplementedError` for one of its required methods.

This mismatch leaks into composition. `build_pipeline()` uses callable
subscriptions and therefore cannot accept every object typed as `EventBus`.
`LedgerRecorderAgent` also subscribes itself during construction, preventing
its `RoutedAgent` wrapper from safely using a publish-only AutoGen adapter.

## Decision

Segregate publishing from local callable subscription.

```python
class EventBus(ABC):
    @abstractmethod
    async def publish(self, topic: str, event: Any) -> None: ...


class SubscribableEventBus(EventBus, ABC):
    @abstractmethod
    def subscribe(self, topic: str, handler: Handler) -> None: ...
```

`InMemoryEventBus` implements `SubscribableEventBus`. `AutoGenEventBus`
implements only `EventBus` and exposes no callable `subscribe()` method.
AutoGen subscription remains explicit through `subscribe_type(runtime,
topic, agent_type)` and `TypeSubscription`.

## Composition and recorder wiring

`build_pipeline()` is an in-memory/callable composition and therefore accepts
`SubscribableEventBus | None`, defaulting to `InMemoryEventBus`. Passing a
publish-only bus fails immediately at the composition boundary with a clear
`TypeError` rather than later through `NotImplementedError`.

`LedgerRecorderAgent` stops subscribing itself inside `__init__`. A public
function performs local wiring:

```python
def subscribe_recorder(bus: SubscribableEventBus, recorder: LedgerRecorderAgent) -> None:
    for event_type, handler_name in RECORDER_SUBSCRIPTION_SPEC:
        bus.subscribe(topic_for(event_type), getattr(recorder, handler_name))
```

The in-memory pipeline invokes this function after constructing the recorder.
Recorder-focused tests invoke it through their `make_recorder()` fixture.
`LedgerRecorderRoutedAgent` does not invoke it because AutoGen delivers its
events through decorated message handlers and external `TypeSubscription`s.

The subscription specification remains the single source for both local
wiring and `RECORDER_TOPICS`; it becomes public under the stable name
`RECORDER_SUBSCRIPTION_SPEC` because composition tests may assert its coverage.

## Dependency rules

Business agents that only publish continue to depend on `EventBus`. Code that
calls `subscribe()` must depend on `SubscribableEventBus`. This includes the
in-memory pipeline composition and test collectors. No business-domain module
imports AutoGen Core.

The architecture fitness test gains an AST rule: outside the runtime adapter
and bootstrap, production code may call `.subscribe()` only where its bus is
explicitly part of in-memory composition. This design does not attempt static
type inference; contract tests cover the concrete composition boundary.

## Error handling

- `AutoGenEventBus.subscribe` is removed, so unsupported behavior cannot be
  invoked through its public API.
- `build_pipeline(bus=publish_only_bus)` raises `TypeError` before constructing
  agents or subscribing handlers.
- Calling `subscribe_recorder()` with a publish-only object raises `TypeError`
  with a message naming `SubscribableEventBus`.
- Publishing behavior and topic-source correlation remain unchanged.

## Compatibility

- Existing callers using `InMemoryEventBus` keep the same `subscribe()` API.
- Existing business-agent constructors keep accepting `EventBus` because they
  only publish.
- `build_runtime_and_bus()` continues returning `(runtime, AutoGenEventBus)`.
- `subscribe_type()` remains the only supported AutoGen subscription helper.
- No new dependency is introduced.

## Testing

1. A contract test proves `AutoGenEventBus` has no callable `subscribe`
   capability and remains an `EventBus`.
2. A contract test proves `InMemoryEventBus` is a `SubscribableEventBus` and
   still delivers handlers.
3. A pipeline test passes a publish-only fake and expects an immediate
   `TypeError` without partial wiring.
4. Recorder tests prove explicit `subscribe_recorder()` wiring preserves all
   lifecycle behavior.
5. The real AutoGen integration tests prove `TypeSubscription` delivery still
   works end to end.
6. The full pytest suite and `compileall` remain green.

## Non-goals

- Building a dynamic callable-to-RoutedAgent bridge.
- Replacing AutoGen `TypeSubscription` semantics.
- Splitting the Recorder or Pipeline modules beyond extracting subscription
  ownership.
- Changing event delivery guarantees, topic names, correlation keys, or
  shutdown semantics.
