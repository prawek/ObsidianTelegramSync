# Как выпустить релиз

Для первого релиза подготовлена версия `v0.1.0`. Все команды ниже выполняются
в корне репозитория. Рабочая ветка проекта сейчас называется `master`.

## Проверка перед выпуском

Запустите тесты в установленном виртуальном окружении:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
git status --short
```

На Linux / macOS замените путь к Python на `.venv/bin/python`.
Проверьте описание в `docs/releases/v0.1.0.md` и `CHANGELOG.md`.
При фактическом выпуске замените заголовок кандидата в CHANGELOG на номер
версии и дату публикации. `.env` и каталог `data/` не должны попадать в коммит.

## Отправка файлов и тега

Сначала сохраните подготовленные файлы в Git:

```powershell
git add README.md CHANGELOG.md docs .github .gitignore .dockerignore
git diff --cached --stat
git commit -m "Prepare v0.1.0 release and repository documentation"
git push origin master
```

Убедитесь, что workflow **Tests** прошёл в разделе Actions.
Затем создайте аннотированный тег на проверенном коммите:

```powershell
git tag -a v0.1.0 -m "Obsidian Telegram Sync v0.1.0"
git push origin v0.1.0
```

Workflow **Release draft** снова проверит код именно из тега на Windows
и Linux с Python 3.12 и 3.14. После успешных проверок он создаст черновик
релиза с описанием из `docs/releases/v0.1.0.md`.
Для создания черновика используется встроенный `GITHUB_TOKEN`;
личный токен GitHub и Telegram-токен в Actions не нужны.

Откройте [Releases](https://github.com/PraWek/ObsidianTelegramSync/releases),
проверьте черновик и нажмите **Publish release**.
Для предварительной версии можно включить **This is a pre-release**.
GitHub добавляет архивы исходников ZIP и tar.gz к релизу.

Если черновик не появился, посмотрите ошибку в Actions. Организационные
правила могут запрещать workflow создавать релизы; тогда используйте
ручной способ ниже.

## Через интерфейс GitHub

После отправки коммитов выберите **Releases → Draft a new release**.
В поле тега задайте `v0.1.0`, а в Target выберите нужный коммит ветки `master`.
Заголовок: `v0.1.0 · Obsidian → Telegram Sync`.
Вставьте содержимое `docs/releases/v0.1.0.md` и сохраните черновик
или опубликуйте релиз. Если тег уже существует, выбирайте его.
Этот способ сам по себе не требует успешного CI: сначала проверьте Actions.

## Через GitHub CLI

Если установлен `gh`, после отправки тега можно создать черновик вручную:

```powershell
gh auth login
gh release create v0.1.0 --verify-tag --draft --title "v0.1.0 · Obsidian → Telegram Sync" --notes-file docs/releases/v0.1.0.md
```

Используйте этот способ вместо автоматического создания, если workflow
не создал черновик. Если черновик уже существует, откройте его в Releases.

## Оформление страницы репозитория

В блоке **About** на главной странице укажите описание:

> Sync an Obsidian vault to Telegram forum topics, with wikilinks and access control.

Подходящие Topics: `obsidian`, `telegram`, `telegram-bot`, `python`,
`markdown`, `note-taking`, `synchronization`.
Добавьте реальный скриншот темы и навигации в README, когда он будет готов.
Перед добавлением LICENSE выберите условия, на которых хотите распространять код.
Лицензия в этом наборе файлов не назначена.

## Следующие версии

Создайте файл `docs/releases/v0.1.1.md`, обновите CHANGELOG и отправьте
новый тег `v0.1.1` на проверенном коммите. Не переносите опубликованные теги
на другой коммит.

Если файла описания нет, workflow использует автоматически сгенерированные
release notes. Их разделы задаёт `.github/release.yml` по меткам pull request.
Теги с суффиксом, например `v0.2.0-beta.1`, создают черновик с отметкой pre-release.

Справка: [GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository),
[автоматические release notes](https://docs.github.com/en/repositories/releasing-projects-on-github/automatically-generated-release-notes),
[gh release create](https://cli.github.com/manual/gh_release_create).
