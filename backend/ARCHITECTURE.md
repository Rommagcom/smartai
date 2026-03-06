# SmartAi Backend — Архитектура

Детальное описание архитектуры, потоков данных и маршрутизации.

## Оглавление
- [Логическая структура проекта](#логическая-структура-проекта)
- [Стек технологий](#стек-технологий)
- [Граф обработки сообщений (LangGraph)](#граф-обработки-сообщений-langgraph)
- [Жизненный цикл запроса](#жизненный-цикл-запроса)
- [Маршрутизация инструментов](#маршрутизация-инструментов)
- [Dynamic Tool Injection](#dynamic-tool-injection)
- [Система памяти](#система-памяти)
- [LLM Provider (LiteLLM)](#llm-provider-litellm)
- [MCP Server](#mcp-server)
- [Guardrails](#guardrails)
- [Docker-инфраструктура](#docker-инфраструктура)
- [Сервисный слой](#сервисный-слой)

---

## Логическая структура проекта

```mermaid
graph TB
    subgraph "backend/"
        direction TB
        subgraph "app/ — Ядро приложения"
            direction LR
            MAIN["main.py<br/>FastAPI + lifespan"]
            subgraph "api/v1/endpoints/"
                AUTH_EP["auth"]
                CHAT_EP["chat"]
                MEM_EP["memory"]
                CRON_EP["cron"]
                DOC_EP["documents"]
                INT_EP["integrations"]
                OBS_EP["observability"]
                TG_EP["telegram_access"]
                WS_EP["websocket"]
                USER_EP["users"]
            end
            subgraph "graph/"
                GRAPH_INIT["__init__.py<br/>StateGraph + compile"]
                GRAPH_NODES["nodes.py<br/>7 узлов графа"]
            end
            subgraph "llm/"
                LLM_PROV["__init__.py<br/>LiteLLM Provider"]
            end
            subgraph "memory/"
                MEM_MGR["__init__.py<br/>MemoryManager"]
            end
            subgraph "mcp/"
                MCP_SRV["__init__.py<br/>MCP Server"]
            end
            subgraph "guardrails/"
                GUARD["__init__.py<br/>Input/Output guardrails"]
            end
            subgraph "services/ — 25 сервисов"
                CHAT_SVC["chat_service"]
                TOOL_ORCH["tool_orchestrator"]
                DYN_SVC["dynamic_tool_service"]
                SKILL_REG["skills_registry"]
                MEM_SVC["memory_service"]
                RAG_SVC["rag_service"]
                STM_SVC["short_term_memory"]
                SCHED_SVC["scheduler_service"]
                WORKER_SVC["worker_service"]
                OTHER_SVC["... +16 сервисов"]
            end
            subgraph "models/"
                MODELS["SQLAlchemy ORM<br/>10 моделей"]
            end
            subgraph "workers/"
                WORKER["worker_service<br/>Redis queue + handlers"]
            end
        end
        subgraph "integrations/"
            direction LR
            TG_BOT["telegram/<br/>adapter + bridge"]
            BASE_MSG["base/<br/>MessengerAdapter"]
        end
        subgraph "alembic/"
            MIGRATIONS["5 миграций"]
        end
        subgraph "scripts/"
            SMOKE["12 smoke-тестов"]
        end
    end

    MAIN --> GRAPH_INIT
    MAIN --> LLM_PROV
    MAIN --> MCP_SRV
    CHAT_EP --> CHAT_SVC
    CHAT_SVC --> GRAPH_INIT
    GRAPH_INIT --> GRAPH_NODES
    GRAPH_NODES --> LLM_PROV
    GRAPH_NODES --> MEM_MGR
    GRAPH_NODES --> TOOL_ORCH
    GRAPH_NODES --> GUARD
    TOOL_ORCH --> DYN_SVC
    TOOL_ORCH --> SKILL_REG
    MEM_MGR --> MEM_SVC
    MEM_MGR --> STM_SVC
    MEM_MGR --> RAG_SVC
    TG_BOT --> BASE_MSG
```

---

## Стек технологий

| Компонент | Технология | Назначение |
|-----------|-----------|------------|
| **Фреймворк** | FastAPI 0.116 + Uvicorn | HTTP/WebSocket API |
| **Граф агента** | LangGraph 1.0 | Оркестрация цепочки обработки сообщений |
| **LLM** | LiteLLM 1.82 | Унифицированный доступ к 100+ LLM-провайдерам |
| **Схемы** | Pydantic v2 | Валидация данных, структурированные ответы LLM |
| **MCP** | MCP Python SDK 1.26 | Model Context Protocol — публикация инструментов |
| **ORM** | SQLAlchemy 2.0 (async) | PostgreSQL + pgvector |
| **Векторная БД** | Milvus 2.3 | RAG-поиск по документам |
| **Кэш/Очередь** | Redis 7.4 | STM, worker queue, WebSocket fanout |
| **Планировщик** | APScheduler | Cron-задачи, напоминания |
| **LLM inference** | Ollama (опционально) | Локальный запуск моделей |
| **Контейнеры** | Docker Compose | Оркестрация всего стека |

---

## Граф обработки сообщений (LangGraph)

Центральный механизм обработки — `StateGraph` из LangGraph. Каждое сообщение проходит через
цепочку из 7 узлов с условной маршрутизацией.

### Узлы графа

| Узел | Функция | Назначение |
|------|---------|------------|
| `guardrail` | `input_guardrail_node` | Проверка на prompt injection |
| `memory` | `memory_node` | Сбор контекста из всех слоёв памяти |
| `router` | `router_node` | LLM решает: tool / chat / clarify |
| `tool_exec` | `tool_execution_node` | Исполнение цепочки инструментов |
| `chat` | `chat_node` | Генерация ответа через LLM |
| `compose` | `compose_node` | Формирование ответа из результатов tool |
| `output` | `output_node` | Output guardrail + запись в STM |

### Диаграмма графа

```mermaid
stateDiagram-v2
    [*] --> guardrail

    guardrail --> output : BLOCK (injection detected)
    guardrail --> memory : PASS

    memory --> router

    router --> tool_exec : decision = "tool"
    router --> chat : decision = "chat" / "memory" / "clarify"
    router --> output : next_step = "end"

    tool_exec --> compose
    chat --> output
    compose --> output

    output --> [*]
```

### GraphState — состояние графа

```mermaid
classDiagram
    class GraphState {
        +UUID user_id
        +UUID session_id
        +str user_message
        +str system_prompt
        +list~str~ permissions
        +list~dict~ history_messages
        +list~str~ stm_context
        +list~str~ ltm_context
        +list~str~ rag_context
        +str history_summary
        +list~ExtractedEntity~ extracted_entities
        +RouterOutput router_output
        +list~ToolResult~ tool_results
        +list~dict~ artifacts
        +GuardrailResult input_guardrail
        +GuardrailResult output_guardrail
        +str final_answer
        +list~dict~ tool_calls_log
        +str next_step
        +int iteration
        +int max_iterations
        +str error
    }
```

---

## Жизненный цикл запроса

От входящего сообщения (REST / WebSocket / Telegram) до ответа пользователю:

```mermaid
sequenceDiagram
    actor User
    participant Client as Telegram / WS / REST
    participant API as POST /api/v1/chat
    participant Chat as ChatService
    participant Graph as LangGraph Agent
    participant Guard as Guardrails
    participant Mem as MemoryManager
    participant Router as Router Node
    participant Tools as ToolOrchestrator
    participant LLM as LiteLLM Provider
    participant DB as PostgreSQL
    participant Redis as Redis
    participant Milvus as Milvus

    User ->> Client: Сообщение
    Client ->> API: HTTP POST / WS message
    API ->> Chat: respond_via_graph()

    Chat ->> Graph: ainvoke(GraphState)

    Graph ->> Guard: input_guardrail_node()
    Guard -->> Graph: PASS / BLOCK

    alt BLOCK
        Graph -->> Chat: "Запрос заблокирован"
    end

    Graph ->> Mem: memory_node()
    par Параллельный сбор контекста
        Mem ->> DB: История + LTM
        Mem ->> Redis: STM (последние обмены)
        Mem ->> Milvus: RAG (семантический поиск)
    end
    Mem -->> Graph: context gathered

    Graph ->> Router: router_node()
    Router ->> LLM: chat_structured(RouterOutput)
    LLM -->> Router: decision + tool_calls / answer

    alt decision = "tool"
        Graph ->> Tools: tool_execution_node()
        Tools ->> Tools: execute_tool_chain()
        Tools -->> Graph: tool_results
        Graph ->> Graph: compose_node()
    else decision = "chat"
        Graph ->> LLM: chat(messages)
        LLM -->> Graph: response text
    end

    Graph ->> Guard: output_guardrail (check_output)
    Graph ->> Redis: append_stm()
    Graph ->> DB: save message

    Graph -->> Chat: final_answer
    Chat -->> API: response
    API -->> Client: JSON / WS event
    Client -->> User: Ответ
```

---

## Маршрутизация инструментов

`ToolOrchestratorService` — центральный диспетчер инструментов. Поддерживает цепочки до 3 шагов.

```mermaid
flowchart TD
    START["router_node():<br/>decision = tool"] --> PLAN["plan_tool_calls()<br/>Формирование цепочки"]
    PLAN --> CHECK{"Тип инструмента?"}

    CHECK -->|"dyn:*"| DYN["DynamicToolService<br/>call_dynamic_tool()"]
    CHECK -->|"pdf_create"| PDF["pdf_service"]
    CHECK -->|"execute_python"| SANDBOX["sandbox_service"]
    CHECK -->|"memory_*"| MEMORY["memory_service"]
    CHECK -->|"cron_*"| CRON["scheduler_service"]
    CHECK -->|"integration_*"| INTEG["integration services"]
    CHECK -->|"doc_search"| RAG["rag_service"]
    CHECK -->|"worker_enqueue"| WORKER["worker_service"]

    DYN --> RESULT["tool_results"]
    PDF --> RESULT
    SANDBOX --> RESULT
    MEMORY --> RESULT
    CRON --> RESULT
    INTEG --> RESULT
    RAG --> RESULT
    WORKER --> RESULT

    RESULT --> COMPOSE["compose_node():<br/>LLM форматирует ответ"]

    subgraph "Skills Registry (27 навыков)"
        direction LR
        S1["pdf_create"]
        S2["execute_python"]
        S3["memory_add/list/search/delete"]
        S4["cron_add/list/delete"]
        S5["integration_add/call/health"]
        S6["doc_search"]
        S7["worker_enqueue"]
        S8["dynamic_tool_*"]
    end

    PLAN -.->|"source of truth"| S1
```

### Зарегистрированные навыки (27)

| Категория | Навыки |
|-----------|--------|
| **Файлы** | `pdf_create` |
| **Sandbox** | `execute_python` |
| **Память** | `memory_add`, `memory_list`, `memory_search`, `memory_delete`, `memory_delete_all` |
| **RAG** | `doc_search` |
| **Планировщик** | `cron_add`, `cron_list`, `cron_delete`, `cron_delete_all` |
| **Worker** | `worker_enqueue` |
| **Интеграции** | `integration_add`, `integrations_list`, `integrations_delete_all`, `integration_call`, `integration_health` |
| **Onboarding** | `integration_onboarding_connect`, `integration_onboarding_test`, `integration_onboarding_save` |
| **Dynamic Tools** | `dynamic_tool_register`, `dynamic_tool_call`, `dynamic_tool_list`, `dynamic_tool_delete`, `dynamic_tool_delete_all` |

---

## Dynamic Tool Injection

Пользователь может в чате подключить произвольный внешний API — ассистент сам создаст инструмент и будет его вызывать.

```mermaid
sequenceDiagram
    actor User
    participant Chat as ChatService
    participant Router as Router Node
    participant DynSvc as DynamicToolService
    participant LLM as LiteLLM
    participant DB as PostgreSQL
    participant HTTP as api_executor

    Note over User,HTTP: Фаза 1: Регистрация

    User ->> Chat: "Подключи API курса валют НБ РК:<br/>https://nationalbank.kz/rss/get_rates.cfm"
    Chat ->> Router: router_node → tool: dynamic_tool_register
    Router ->> DynSvc: register_from_user_message()
    DynSvc ->> LLM: chat_structured(ApiRegistrationPayload)
    LLM -->> DynSvc: {name, url, method, headers, params, description}
    DynSvc ->> DB: INSERT INTO dynamic_tools
    DynSvc -->> Chat: "Инструмент 'nbk_rates' создан ✓"

    Note over User,HTTP: Фаза 2: Использование

    User ->> Chat: "Какой курс доллара?"
    Chat ->> Router: router_node → tool: dyn:nbk_rates
    Router ->> DynSvc: call_dynamic_tool("nbk_rates")
    DynSvc ->> DB: SELECT FROM dynamic_tools
    DynSvc ->> HTTP: GET https://nationalbank.kz/...
    HTTP -->> DynSvc: XML/JSON response
    DynSvc -->> Chat: tool_result
    Chat -->> User: "Курс USD: 525.73 ₸"
```

### Модель данных Dynamic Tool

```mermaid
erDiagram
    USERS ||--o{ DYNAMIC_TOOLS : owns
    DYNAMIC_TOOLS {
        uuid id PK
        uuid user_id FK
        string name
        string description
        string url
        string method
        json headers
        json params
        json json_body
        json input_schema
        datetime created_at
    }
```

---

## Система памяти

Четырёхуровневая система памяти, собираемая параллельно в `memory_node()`:

```mermaid
flowchart LR
    subgraph "MemoryManager.gather_context()"
        direction TB
        MSG["Сообщение<br/>пользователя"]

        MSG --> |asyncio.create_task| STM
        MSG --> |asyncio.create_task| LTM
        MSG --> |asyncio.create_task| RAG
        MSG --> |asyncio.create_task| HIST
    end

    subgraph STM["STM — Кратковременная"]
        STM_D["Redis FIFO<br/>TTL 24 часа<br/>Последние обмены"]
    end

    subgraph LTM["LTM — Долговременная"]
        LTM_D["PostgreSQL + pgvector<br/>Факты с decay/pin/lock<br/>Семантический поиск"]
    end

    subgraph RAG["RAG — Документы"]
        RAG_D["Milvus<br/>Загруженные документы<br/>Семантический поиск"]
    end

    subgraph HIST["История"]
        HIST_D["PostgreSQL<br/>Сообщения сессии<br/>+ сжатие старых"]
    end

    STM --> CTX["Обогащённый<br/>system_prompt"]
    LTM --> CTX
    RAG --> CTX
    HIST --> CTX

    CTX --> ROUTER["Router Node"]
```

### Извлечение сущностей

`MemoryManager.extract_entities()` — из текста извлекаются:
- **timezone** (regex, затем LLM fallback)
- **city** (regex)
- **name** (regex)

Сущности с confidence ≥ 0.7 сохраняются как LTM-факты.

---

## LLM Provider (LiteLLM)

Единый интерфейс для работы с любыми LLM через `LiteLLM`:

```mermaid
flowchart TD
    subgraph "LLMProvider (singleton)"
        CHAT["chat()<br/>→ str"]
        STREAM["stream_chat()<br/>→ AsyncGenerator"]
        STRUCT["chat_structured()<br/>→ Pydantic model"]
        EMBED["embeddings()<br/>→ list[float]"]
    end

    CHAT --> RESOLVE["_resolve_model()"]
    STREAM --> RESOLVE
    STRUCT --> RESOLVE
    EMBED --> RESOLVE

    RESOLVE --> |"openai/*"| OPENAI["OpenAI API"]
    RESOLVE --> |"anthropic/*"| ANTH["Anthropic API"]
    RESOLVE --> |"ollama_chat/*"| OLLAMA["Ollama (локальный)"]
    RESOLVE --> |"azure/*"| AZURE["Azure OpenAI"]
    RESOLVE --> |"groq/*"| GROQ["Groq"]
    RESOLVE --> |"bedrock/*"| BEDROCK["AWS Bedrock"]
    RESOLVE --> |"другие"| OTHER["100+ провайдеров"]

    subgraph "Семафор"
        SEM["asyncio.Semaphore<br/>(MAX_CONCURRENCY)"]
    end

    CHAT --> SEM
    STREAM --> SEM
    STRUCT --> SEM
```

### Методы

| Метод | Возвращает | Назначение |
|-------|-----------|------------|
| `chat()` | `str` | Текстовый ответ LLM |
| `stream_chat()` | `AsyncGenerator[str]` | Потоковые токены |
| `chat_structured()` | `Pydantic model T` | Структурированный ответ (JSON mode + retry) |
| `embeddings()` | `list[float]` | Векторные эмбеддинги |

---

## MCP Server

SmartAi публикует свои инструменты через **Model Context Protocol** — стандартный протокол
для интеграции с внешними AI-клиентами (Claude Desktop, Cursor и др.).

```mermaid
flowchart LR
    subgraph "Внешние клиенты"
        CLAUDE["Claude Desktop"]
        CURSOR["Cursor"]
        OTHER["Другие MCP-клиенты"]
    end

    subgraph "SmartAi MCP Server"
        LIST["list_tools()<br/>→ 27 инструментов"]
        CALL["call_tool(name, args)<br/>→ результат"]
    end

    subgraph "Внутренние сервисы"
        SKILLS["skills_registry_service"]
        ORCH["tool_orchestrator_service"]
    end

    CLAUDE --> LIST
    CURSOR --> LIST
    OTHER --> LIST

    CLAUDE --> CALL
    CURSOR --> CALL

    LIST --> SKILLS
    CALL --> ORCH
```

---

## Guardrails

Двухуровневая защита: на входе и выходе.

```mermaid
flowchart TD
    INPUT["Входное сообщение"] --> IG["Input Guardrail"]

    IG --> |"12+ regex-паттернов"| CHECK1{"Prompt injection?"}
    CHECK1 --> |"Да"| BLOCK1["BLOCK<br/>Запрос отклонён"]
    CHECK1 --> |"Нет"| PASS1["PASS → memory"]

    subgraph "Проверки входа"
        P1["Role hijacking:<br/>ignore previous, forget rules,<br/>you are now DAN"]
        P2["Prompt extraction:<br/>repeat your prompt,<br/>show instructions"]
        P3["Delimiter injection:<br/>```system```, [SYSTEM],<br/>&lt;|im_start|&gt;"]
    end

    IG -.-> P1
    IG -.-> P2
    IG -.-> P3

    ANSWER["Ответ LLM"] --> OG["Output Guardrail"]
    OG --> CHECK2{"Утечка / injection?"}
    CHECK2 --> |"Да"| STRIP["Strip dangerous content<br/>→ WARN / BLOCK"]
    CHECK2 --> |"Нет"| PASS2["PASS → пользователю"]

    subgraph "Проверки выхода"
        P4["Leaked prompt:<br/>my system prompt is"]
        P5["Injected calls:<br/>&lt;function_calls&gt;, &lt;invoke&gt;"]
    end

    OG -.-> P4
    OG -.-> P5
```

---

## Docker-инфраструктура

```mermaid
graph TB
    subgraph "Docker Compose Stack"
        direction TB

        subgraph "Application Layer"
            API["api<br/>:8000<br/>FastAPI + Uvicorn"]
            SCHED["scheduler-leader<br/>:8010<br/>APScheduler<br/>(profile: multi)"]
            WORKER_D["worker ×N<br/>Redis queue consumer<br/>(profile: multi)"]
            TG["telegram-bot<br/>python-telegram-bot"]
        end

        subgraph "Data Layer"
            PG["postgres<br/>:5432<br/>pgvector/pgvector:pg16"]
            REDIS_D["redis<br/>:6379<br/>redis:7.4-alpine"]
        end

        subgraph "Vector / ML Layer"
            MILVUS["milvus-standalone<br/>:19530<br/>milvusdb/milvus:v2.3.3"]
            ETCD["etcd<br/>Milvus metadata"]
            MINIO["minio<br/>:9000<br/>Milvus storage"]
            OLLAMA_D["ollama<br/>:11434<br/>LLM inference"]
        end

        subgraph "Monitoring"
            PROM["prometheus<br/>:9090<br/>Metrics + alerts"]
        end
    end

    API --> PG
    API --> REDIS_D
    API --> MILVUS
    API --> OLLAMA_D
    SCHED --> PG
    SCHED --> REDIS_D
    WORKER_D --> PG
    WORKER_D --> REDIS_D
    TG --> API
    PROM --> API
    MILVUS --> ETCD
    MILVUS --> MINIO

    subgraph "Volumes"
        V1["postgres_data"]
        V2["milvus_data"]
        V3["ollama_models"]
    end

    PG --> V1
    MILVUS --> V2
    OLLAMA_D --> V3
```

### Multi-instance масштабирование

```
┌─────────────┐   ┌──────────────────┐   ┌─────────────┐
│  api ×N     │   │ scheduler-leader │   │ worker ×N   │
│ HTTP/WS     │   │ (1 экземпляр)    │   │ Redis queue │
│ WORKER=off  │   │ SCHEDULER=on     │   │ WORKER=on   │
│ SCHED=off   │   │ WORKER=off       │   │ SCHED=off   │
└──────┬──────┘   └────────┬─────────┘   └──────┬──────┘
       │                   │                      │
       └───────────┬───────┴──────────────────────┘
                   │
           ┌───────┴───────┐
           │ Redis + PG    │
           └───────────────┘
```

---

## Сервисный слой

25 сервисов в `app/services/`:

```mermaid
flowchart TD
    subgraph "Оркестрация"
        CS["chat_service<br/>respond_via_graph()"]
        TOS["tool_orchestrator_service<br/>execute_tool_chain()"]
        SRS["skills_registry_service<br/>27 навыков"]
        DTS["dynamic_tool_service<br/>Dynamic Tool Injection"]
    end

    subgraph "Память и контекст"
        MS["memory_service<br/>LTM + pgvector"]
        STMS["short_term_memory_service<br/>Redis STM"]
        RS["rag_service<br/>Milvus RAG"]
    end

    subgraph "Планировщик"
        SS["scheduler_service<br/>APScheduler cron"]
        SPS["schedule_parser_service<br/>NL → cron"]
    end

    subgraph "Исполнение"
        AE["api_executor<br/>HTTP вызовы"]
        SBS["sandbox_service<br/>Python sandbox"]
        PS["pdf_service<br/>PDF генерация"]
        WS["worker_service<br/>Фоновые задачи"]
    end

    subgraph "Безопасность"
        ADS["auth_data_security<br/>Fernet шифрование"]
        EPS["egress_policy<br/>Sandbox firewall"]
    end

    subgraph "Доставка"
        WSM["websocket_manager<br/>WS fanout"]
        WRS["worker_result_service<br/>Результаты задач"]
        DFS["delivery_format<br/>Формат payload"]
    end

    subgraph "Прочее"
        HC["http_client_service"]
        AS["alerting_service"]
        OMS["observability_metrics"]
        SIS["self_improvement"]
        SOS["soul_service<br/>Персона"]
        IOS["integration_onboarding"]
        MVS["milvus_service"]
    end

    CS --> TOS
    TOS --> SRS
    TOS --> DTS
    DTS --> AE
    TOS --> AE
    TOS --> SBS
    TOS --> PS
    TOS --> MS
    TOS --> RS
    TOS --> SS
    CS --> MS
    CS --> STMS
    CS --> RS
```
