# Целевая Windows/WSL: первые предоставленные результаты

Дата: **28.10.2026, 23:01 по локальному времени целевой машины**, как указал пользователь. Источник — **переданный пользователем терминальный вывод, пересланный Org**. Planner не запускал эти команды на Windows и не измерял машину самостоятельно. Ниже сохранены сообщённые поля; это не реконструкция полного stdout. Commit клона, WSL version/distro и часовой пояс target в сообщении не приведены.

## Зафиксированные сведения

| Наблюдение | Переданное значение | Что подтверждено этим выводом |
| --- | --- | --- |
| Клонирование проекта | `main` успешно клонирован в WSL | Получение репозитория; это не проверка offline restore, точный commit ещё не записан |
| Synthetic runner suite | **10 tests PASS, 4.296 s** | Пользователь запустил синтетические проверки runner на target WSL; это работа с заглушками, не inference модели |
| NVIDIA-SMI | **610.57.01** | Утилита доступна и показала GPU сведения в guest |
| KMD / CUDA UMD | **610.88 / 13.3** | Именно сообщённые driver/UMD поля; наличие CUDA Toolkit/compiler или llama.cpp CUDA execution этим не установлено |
| GPU | **RTX 3060, 12288 MiB total, 718 MiB used** | GPU виден из данного WSL окружения; расход в момент снимка, не пик модели |
| Процесс GPU | `/Xwayland`, per-process memory **N/A** | Отдельная память процесса недоступна в переданном выводе; N/A не превращается в ноль |
| `free -h`: Mem | **7.7 Gi total, 793 Mi used, 6.9 Gi available** | Около 8 GB-класса доступной guest памяти на момент снимка; точный configured ceiling не установлен |
| `free -h`: Swap | **2.0 Gi total, unused** | Наличие swap без использования в этом снимке; не доказательство отсутствия будущих stalls |
| `df`: `/dev/sdd` на `/` | **1007G size, 3.6G used, 953G avail** | Как напечатаны единицы guest filesystem; это не физическое свободное место Windows или allocated размер VHDX |

## Статусы и решения

- Target synthetic suite: **PASS по пользовательскому выводу**. Агент не повторял suite; 4.296 s — время синтетических тестов, не latency/tokens per second модели.
- GPU visibility и guest RAM/filesystem snapshot: **данные получены от пользователя**. WSL version, distro, kernel/architecture/libc, текущие build/runtime tools, Windows build/free memory, host volumes и physical VHDX accounting ещё не записаны.
- llama.cpp CUDA inference, GGUF/projector pairing, TTFT/throughput, peak RAM/VRAM под моделью, code/RU/vision quality, offline isolation и cold restore: **not run / результатов нет**.
- Сохранить текущее окружение около 8 GB как первый baseline. Не требуется менять RAM сейчас. Возможное сравнение ceiling 8/10 GB остаётся поздним отдельным опытом, без правок memory/swap или shutdown в этом шаге.
- Ограничение всего продукта **100000000000 bytes** сохраняется. Guest `953G avail` не заменяет 100 GB product ledger и не доказывает наличие такого физического остатка на Windows.

## Следующий ручной шаг: только недостающие read-only сведения

Сначала передать Org сведения из **Windows PowerShell**; команды не устанавливают компоненты и не меняют WSL:

```powershell
wsl --version
wsl --status
wsl -l -v
Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber, TotalVisibleMemorySize, FreePhysicalMemory
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | Select-Object DeviceID, Size, FreeSpace
```

`Size`/`FreeSpace` нужны по физически размещающему VHDX/будущие артефакты Windows-тому; `FreePhysicalMemory` не выдаётся за точный Available RAM counter. Путь и физически выделенный размер product VHDX затем вносятся в отдельную ведомость: ни guest `df`, ни обычная длина файла сами по себе её не закрывают. Если новая WSL diagnostic command не поддерживается, сохранить ошибку без auto-update.

В текущем **WSL shell**, для определения distro и доступного способа подготовки runtime:

```sh
cat /etc/os-release
uname -m
getconf GNU_LIBC_VERSION
command -v llama-cli llama-server llama-mtmd-cli cmake gcc g++ make ninja nvcc python3 git
```

Отсутствие команды в PATH не доказывает отсутствие portable бинарника в другом каталоге. Неподдерживаемый `getconf` тоже сохранить как результат; distro не предполагается Ubuntu. Версии `cmake --version`, `g++ --version`, `nvcc --version` нужны только для найденных инструментов. Из каталога клонированного **local-ai-guru** отдельно записать:

```sh
git rev-parse HEAD
```

Этого достаточно для следующего решения Org: совместимый готовый Linux CUDA runtime или ограниченная подготовка сборки, с точным перечнем prerequisites и объёмом. Сейчас **не выполнять** установку, загрузку runtime/моделей или конфигурацию WSL. Первый модельный shortlist уже выбран: Qwen3.5-9B и обязательное сравнение Gemma 4 E4B; новый survey не требуется. Точный preparation/download лот формируется после недостающих сведений и отдельного разрешённого шага.

План и gates: [PLAN.md](../../PLAN.md); модельный состав и byte evidence: [models.md](models.md), [offload.md](offload.md).
