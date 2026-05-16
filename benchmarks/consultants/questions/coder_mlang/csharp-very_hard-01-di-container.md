---
id: csharp-very_hard-01-di-container
tier: very_hard
source: csharp-idiom
task: 'Minimal DI container via reflection: constructor resolution + singleton cache + cycle detection (CircularDependencyException).'
sandbox_path: solution.cs
oracle: csharp-very_hard-01-di-container-oracle.py
---

# csharp-very_hard-01-di-container

Implement a minimal **dependency-injection container** in C#
with constructor resolution and cycle detection.

## API

```csharp
public class Container {
    // Register `TImpl` as the concrete type to instantiate when
    // `TService` is requested. TImpl must satisfy TService and
    // have exactly one public constructor.
    public void Register<TService, TImpl>() where TImpl : TService;

    // Resolve TService. Container walks the constructor's
    // parameters and recursively resolves each. Singleton-style
    // semantics: the same instance is returned across calls
    // within one container.
    public T Resolve<T>();
}

public class CircularDependencyException : Exception {
    public CircularDependencyException(string message)
        : base(message) {}
}
```

## Required behavior

- `Register<TService, TImpl>()`: stores the type mapping.
- `Resolve<T>()`:
  1. Look up the registered implementation type for `T`.
  2. Find its single public constructor.
  3. For each parameter type, recursively `Resolve<...>()`.
  4. Invoke the constructor with the resolved arguments.
  5. Cache the instance — subsequent `Resolve<T>()` returns the
     same object.
- **Cycle detection**: if `A` depends on `B` and `B` depends on
  `A`, `Resolve<A>()` throws `CircularDependencyException` with
  a message naming both types. Use a "currently-resolving" set
  during the recursive walk.
- Resolving an unregistered type throws
  `InvalidOperationException`.

## I/O contract (driver in main)

The driver constructs a Container, registers a fixed set of
test types, then prints diagnostic lines:

```csharp
public interface ILogger { string Tag { get; } }
public interface IRepo { string Name { get; } }
public interface IService { string Describe(); }

public class ConsoleLogger : ILogger {
    public string Tag => "console";
}

public class UserRepo : IRepo {
    private readonly ILogger _logger;
    public UserRepo(ILogger logger) { _logger = logger; }
    public string Name => $"users@{_logger.Tag}";
}

public class UserService : IService {
    private readonly IRepo _repo;
    private readonly ILogger _logger;
    public UserService(IRepo repo, ILogger logger) {
        _repo = repo; _logger = logger;
    }
    public string Describe() => $"{_repo.Name}/{_logger.Tag}";
}

// Cycle-test types:
public interface IA { }
public interface IB { }
public class CycleA : IA { public CycleA(IB b) {} }
public class CycleB : IB { public CycleB(IA a) {} }
```

`main` runs THIS sequence:

```
var c = new Container();
c.Register<ILogger, ConsoleLogger>();
c.Register<IRepo, UserRepo>();
c.Register<IService, UserService>();

var svc = c.Resolve<IService>();
Console.WriteLine(svc.Describe());           // expect: users@console/console

var svc2 = c.Resolve<IService>();
Console.WriteLine(object.ReferenceEquals(svc, svc2) ? "same" : "different");
// expect: same   (singleton)

// Cycle:
var c2 = new Container();
c2.Register<IA, CycleA>();
c2.Register<IB, CycleB>();
try {
    c2.Resolve<IA>();
    Console.WriteLine("no cycle detected");
} catch (CircularDependencyException) {
    Console.WriteLine("cycle detected");      // expect this
}
```

## Constraints

- Single file `solution.cs`. Built via `dotnet publish -c Release`.
- Use `System.Reflection` (`Type.GetConstructors`,
  `ConstructorInfo.GetParameters`, `Activator.CreateInstance`
  or direct `ConstructorInfo.Invoke`).
- Stdlib only.

