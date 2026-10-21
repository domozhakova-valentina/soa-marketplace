# HW2 — Marketplace API

Это API небольшого маркетплейса. Продавец добавляет товары, покупатель собирает
заказ, а сервис проверяет остатки и считает скидку. Данные хранятся в PostgreSQL.

Используются Python, FastAPI, SQLAlchemy, PostgreSQL, Flyway и Docker Compose.
API и модели запросов генерируются из OpenAPI.

Работают товары, заказы, создание и применение промокодов, JWT, роли и логи.
Промокоды в демонстрации создаются через `POST /promo-codes`.

## 1. Как запустить

Нужен запущенный Docker Desktop с Linux-контейнерами. Все команды ниже выполняются
в **PowerShell**, из папки `hw2`. Если сейчас открыт корень репозитория:

```powershell
cd .\hw2
```

Первый запуск:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\init-env.ps1
docker compose up --build -d
docker compose ps -a
```

Первая команда создаёт `.env` и случайный ключ для JWT. Существующий настроенный
ключ она сохраняет. `.env` и сгенерированные файлы не добавляются в Git.

Сначала запускается PostgreSQL, затем Flyway применяет миграции, потом стартует
API. Статус `Exited (0)` у Flyway нормальный: он выполнил миграции и завершился.

Адреса:

- Проверка работы: <http://localhost:8000/health>.
- Swagger, где можно выполнять запросы: <http://localhost:8000/docs>.
- Документация ReDoc: <http://localhost:8000/redoc>.
- Схема работающего приложения: <http://localhost:8000/openapi.json>.

```powershell
Invoke-RestMethod http://localhost:8000/health
docker compose logs --tail 50 api
```

Ожидаемый ответ health: `{"status":"ok"}`. Если порт 8000 занят, поменяй
`API_PORT` в `.env`, например на 8001. Тогда адреса тоже будут с портом 8001.

После изменения кода:

```powershell
docker compose up --build -d api
```

Остановить проект с сохранением данных:

```powershell
docker compose down
```

## 2. Пользователи и роли

Создать учебные аккаунты:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\seed.ps1
```

Скрипт попросит пароль и создаст `demo_user`, `demo_seller`, `demo_admin` с ролями
USER, SELLER и ADMIN. Повторный запуск не меняет пароли существующих аккаунтов.
При обычной регистрации через API можно получить только USER.

В Swagger выполни `/auth/login`, скопируй `access_token`, нажми **Authorize**
и вставь токен без слова Bearer. В запросах из терминала используется заголовок
`Authorization: Bearer <access_token>`.

| Операция | USER | SELLER | ADMIN |
| --- | --- | --- | --- |
| Читать товары | Все | Все | Все |
| Создавать и менять товары | Нельзя | Свои | Любые |
| Создавать заказы | Можно | Нельзя | Можно |
| Читать, менять и отменять заказы | Свои | Нельзя | Любые |
| Создавать промокоды | 403 | Можно | Можно |

Access token действует 30 минут, refresh — 14 дней. Refresh подходит только
для `/auth/refresh`, использовать его вместо access нельзя.

Основные адреса API:

| Группа | Методы |
| --- | --- |
| Вход | `POST /auth/register`, `/auth/login`, `/auth/refresh` |
| Товары | `POST /products`, `GET /products`, `GET/PUT/DELETE /products/{id}` |
| Заказы | `POST /orders`, `GET/PUT /orders/{id}`, `POST /orders/{id}/cancel` |
| Промокоды | `POST /promo-codes` |

## 3. Где находится код

| Папка или файл | Что там лежит |
| --- | --- |
| `openapi/marketplace.yaml` | Описание адресов API, запросов, ответов, ограничений и ошибок. |
| `openapi/templates/` | Шаблоны генерации, в том числе для Decimal, UUID и проверки ролей. |
| `generated/` | Сгенерированные маршруты и модели. В Git не хранится. |
| `app/api_impl/` | Реализации сгенерированных API-классов. |
| `app/services/` | Проверки и действия с товарами и заказами. |
| `app/repositories/` | SQL-запросы к БД. |
| `app/security/` | Пароли, JWT и проверка прав. |
| `app/middleware/` | Обработка ошибок и JSON-логи. |
| `migrations/` | Три миграции Flyway: таблицы, индексы и триггеры дат. |
| `tests/` | Автоматические проверки. |
| `scripts/` | Генерация, тесты и шаги демонстрации. |

`marketplace.yaml` — это контракт: сначала описывается API, из него получаются
маршруты и модели, затем в `app/` пишется реализация. Модель запроса часто
называют DTO. Вручную такие модели здесь не создаются.

Проверить контракт и отдельно сгенерировать код:

```powershell
docker compose --profile tools run --rm codegen validate -i /workspace/openapi/marketplace.yaml
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\generate.ps1
```

При сборке Docker генерация выполняется автоматически. Python и Java на
компьютере для этого устанавливать не нужно. Генератор закреплён на версии 7.12.0.
Подробное описание контракта есть в [openapi/README.md](openapi/README.md).

## 4. Главные правила

- Удаление товара меняет статус на ARCHIVED. Строка остаётся в БД и доступна для чтения.
- Список товаров поддерживает `page=0`, `size=20`, фильтры status и category.
  Категория сравнивается точно, с учётом регистра. Сортировка: created_at, затем id.
- PUT товара заменяет редактируемые поля полностью. Если description пропущено,
  оно становится null. Владелец и дата создания сохраняются.
- У одного пользователя может быть только один активный заказ: CREATED или PAYMENT_PENDING.
- Количество товара списывается при создании заказа и возвращается при отмене.
- В позиции сохраняется цена на момент заказа. Изменение каталога её не меняет.
  При явном PUT заказа берутся новые текущие цены каталога.
- Редактировать заказ можно только в CREATED, отменять — в CREATED или PAYMENT_PENDING.
  Повторная отмена возвращает 409 и не увеличивает склад ещё раз.
- Один product_id нельзя передать в заказе дважды.

Ошибки имеют общий формат: error_code, message и необязательный details.
Например, неправильный UUID даёт 400 VALIDATION_ERROR с названием поля,
а правильный UUID отсутствующего товара — 404 PRODUCT_NOT_FOUND.
Неизвестный маршрут возвращает NOT_FOUND, неверный HTTP-метод — METHOD_NOT_ALLOWED;
у них такая же структура ответа. stock, page и size ограничены диапазоном int32
до 2147483647. Нулевой символ в текстовых полях отклоняется с 400, поскольку
PostgreSQL не хранит его в строках.

Модель состояний:

```text
CREATED -> PAYMENT_PENDING -> PAID -> SHIPPED -> COMPLETED
CREATED -> CANCELED
PAYMENT_PENDING -> CANCELED
```

Маршруты оплаты и доставки в задании не описаны. Для проверки остальных
состояний используются явные начальные данные и тесты.

Деньги считаются через Decimal, в PostgreSQL используется NUMERIC(12,2).
Скидка округляется до двух знаков по ROUND_HALF_UP: например, 0.005 становится 0.01.
Фиксированная скидка не превышает сумму заказа. Процент до 70 включительно
применяется как скидка. Если процент больше 70, скидка равна нулю и оплачивается
полная сумма — по фразе задания «в ином случае цена 100%».
Проверки промокода и учёт его использования при этом выполняются обычным образом.

Промокоды общие для всех продавцов. Минимум суммы проверяется до скидки.
При редактировании тот же заказ не расходует промокод повторно. Если сумма стала
ниже минимума, скидка снимается, связь с промокодом удаляется, использование
возвращается. Истёкший, ещё не действующий или отключённый промокод даёт 422
и откат обновления. При отмене использование возвращается даже у истёкшего промокода.

### Как создать промокод

В Swagger войди как SELLER или ADMIN и вызови `POST /promo-codes`:

```json
{
  "code": "SAVE_10",
  "discount_type": "PERCENTAGE",
  "discount_value": 10,
  "min_order_amount": 200,
  "max_uses": 100,
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_until": "2027-01-01T00:00:00Z"
}
```

Успех — 201 с сохранённым промокодом и его UUID. current_uses изначально равен 0,
active по умолчанию true. Передавать id или current_uses вручную нельзя.
Для фиксированной скидки укажи FIXED_AMOUNT, а в discount_value — сумму скидки.
Обе даты должны содержать часовой пояс, окончание должно быть позже начала.
Если даты или поля неверные, вернётся 400 VALIDATION_ERROR; если code уже занят —
409 PROMO_CODE_ALREADY_EXISTS. Повторный запрос не меняет существующий промокод.
Значение max_uses — от 1 до 2147483647, по диапазону INTEGER в PostgreSQL.

### Транзакции заказов

Операции с заказом выполняются в одной транзакции `engine.begin()`. Например,
при обновлении сначала возвращаются старые остатки, затем резервируются новые.
Если дальше возникла ошибка, БД откатывает всё: старые позиции, склад, суммы,
счётчик промокода и запись операции остаются прежними.

Для одновременных запросов используются блокировки строк: пользователь → заказ →
товары по отсортированным UUID → промокод. Дополнительно БД запрещает два активных
заказа одного пользователя через частичный уникальный индекс.

## 5. Лимит N

В `.env` есть две независимые настройки:

```dotenv
ORDER_CREATE_INTERVAL_MINUTES=5
ORDER_UPDATE_INTERVAL_MINUTES=5
```

Это минимальный интервал между успешными операциями одного типа для пользователя.
Можно поставить дробное число, например `0.1` — 6 секунд. Значение 0 отключает
только соответствующий частотный лимит. После изменения пересоздай API:

```powershell
docker compose up -d api
```

Проверка частоты идёт раньше проверки активного заказа. Поэтому быстрый повтор
создания обычно даёт **429 ORDER_LIMIT_EXCEEDED**, а не **409 ORDER_HAS_ACTIVE**.
В демонстрации эти ошибки показываются отдельно, без ожидания пяти минут.

## 6. Автоматические проверки

Все проверки с настоящей PostgreSQL:

```powershell
docker compose --profile tools run --build --rm test-integration python -m pytest -q
```

Запускаются отдельная `db-test` и миграции Flyway. Основная БД не используется.
Последний полный прогон: **530 passed, без пропусков**, из них 196 проверок
с настоящей PostgreSQL. Сборка из чистой папки и все шаги демонстрации тоже прошли.
После проверки тестовую БД можно остановить:

```powershell
docker compose stop db-test flyway-test
```

Другие варианты:

```powershell
# Только проверки без PostgreSQL. Тесты БД будут пропущены.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test.ps1

# Только интеграционные проверки БД.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-integration.ps1
```

Что подтверждают тесты:

| Что проверяем | Где это проверяется |
| --- | --- |
| Статусы ответов, UUID, обязательные поля, сгенерированные маршруты и модели | `test_generated_contract.py` |
| Границы name, description, price, stock, category, items, quantity, promo_code | `test_generated_contract.py`, `test_validation_boundaries.py`, `test_order_contract.py` |
| CRUD товаров, фильтры, страницы, ARCHIVED в БД | `test_products_postgres.py`, `test_products.py` |
| Остатки, снимки цен, точная сумма, обе скидки, сроки и лимиты промокода | `test_orders_postgres.py` |
| Создание промокодов SELLER/ADMIN, отказ USER, поля, даты, повторы и гонки | `test_promo_codes_postgres.py` |
| Ошибки одной позиции или промокода и полный откат | `test_orders_postgres.py`, `test_order_mutations_postgres.py` |
| Замена позиций, возврат старого резерва, снятие скидки | `test_order_mutations_postgres.py` |
| Однократная отмена и возврат промокода | `test_order_mutations_postgres.py` |
| Все 36 пар состояний, запрет редактирования и отмены | `test_order_states.py`, `test_order_mutations_postgres.py` |
| JWT: подпись, срок, назначение, refresh и смена роли | `test_auth.py`, `test_auth_postgres.py` |
| Права на товары и заказы, чужой владелец, ADMIN | `test_auth.py`, `test_products.py`, `test_order_mutations_postgres.py` |
| JSON-логи, поля, маскирование, 400/401/403/500, X-Request-Id | `test_request_logging.py`, `test_errors.py` |
| Общий формат 404/405, границы int32, нулевые символы, Unicode и некорректные даты JWT | `test_review_regressions.py` |
| Последний товар, последний промокод, два активных заказа, блокировки | `test_orders_postgres.py`, `test_order_locks_postgres.py`, `test_order_mutations_postgres.py` |

Создание промокодов проверяется настоящими запросами с JWT и записью в PostgreSQL.
В том числе проверяется, что два одновременных запроса создают только одну строку.

Отдельно запустить, например, только конкурентные проверки:

```powershell
docker compose --profile tools run --build --rm test-integration python -m pytest -q tests/test_order_locks_postgres.py tests/test_orders_postgres.py -k "concurrent or parallel or lock"
```

### Сборка из чистой папки

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check-clean-build.ps1
```

Скрипт копирует исходники в новую папку без generated и старого .env, собирает
образ, запускает отдельные API и PostgreSQL, проверяет health и три миграции.
Порт выбирается автоматически. После проверки удаляются только контейнеры и
том этой новой проверки. Рабочая БД проекта сохраняется.

Чтобы заодно прогнать все шаги демонстрации:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check-clean-build.ps1 -RunDemo
```

Копия исходников остаётся в `.venv/verification/` для разбора ошибок. Она игнорируется
Git. Для пересборки Docker-слоёв без кэша можно добавить `-NoCache`.

## 7. Как менять запросы вручную

После Login доступна функция `Invoke-MarketplaceRequest`. Это обычный HTTP-запрос,
в котором можно менять тело и ожидаемый результат. Например, после Cancel:

```powershell
$body = @{
    items = @(@{ product_id = $MarketplaceDemo.ProductId; quantity = 1000 })
}
Invoke-MarketplaceRequest -Method POST -Path /orders `
    -Token $MarketplaceDemo.Tokens.User -Body $body `
    -ExpectedStatus 400 -ExpectedError VALIDATION_ERROR
```

Здесь 1000 превышает ограничение количества, поэтому получаем 400 до бизнес-проверок.
Сразу после Order ещё действует лимит CREATE_ORDER, даже если заказ отменён.
Чтобы показать именно нехватку остатков без этого лимита, используй готовый
Rollback: он создаёт другого покупателя. Для ручной смены параметров удобен и
Swagger: скопируй id товара из ответа и поменяй quantity, price, status или category.

Примеры граничных значений: name из 255 и 256 символов, price 0.01 и 0,
quantity 999 и 1000, пустой items, неправильный UUID, page=-1.
В тестах также есть истёкшие JWT, границы срока промокода, 50 и 51 позиция,
одновременные запросы. Ждать истечения настоящего access 30 минут не нужно.

## 8. База данных и логи

Посмотреть данные независимо от demo.ps1:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\inspect-db.ps1
```

Он выполняет `scripts/inspect-db.sql`: историю миграций, индексы, триггеры,
пользователей без паролей, товары, заказы, позиции, промокоды и операции.
Для свободных SELECT можно открыть psql. Значения имени БД и пользователя
возьми из .env; при настройках по умолчанию:

```powershell
docker compose exec db psql -U marketplace -d marketplace
```

Примеры SQL:

```sql
SELECT id, name, stock, status FROM products ORDER BY updated_at DESC LIMIT 10;
SELECT id, status, total_amount, discount_amount FROM orders ORDER BY updated_at DESC LIMIT 10;
SELECT order_id, product_id, quantity, price_at_order FROM order_items;
SELECT code, current_uses, max_uses FROM promo_codes;
SELECT user_id, operation_type, created_at FROM user_operations ORDER BY created_at DESC LIMIT 10;
```

Логи запросов:

```powershell
docker compose logs -f --no-log-prefix api
```

Один запрос — одна JSON-строка с request_id, method, endpoint, status_code,
duration_ms, user_id и timestamp. request_id совпадает с X-Request-Id в ответе.
Пароли и токены в теле маскируются. Повреждённое тело и тело больше 64 КиБ
показываются как метаданные; само приложение получает исходное тело полностью.