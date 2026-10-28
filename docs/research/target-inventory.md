# Целевая Windows/WSL: первые предоставленные результаты

Дата: **28.10.2026, 23:01 по локальному времени целевой машины**, как указал пользователь. Источник — **переданный пользователем терминальный вывод, пересланный Org**. Planner не запускал эти команды на Windows и не измерял машину самостоятельно. Ниже сохранены сообщённые поля; это не реконструкция полного stdout. Commit клона, WSL version/distro и часовой пояс target в первоначальном сообщении не приведены. Distro, PATH и C: сведения ниже дополнены позднейшим пользовательским выводом; отдельный timestamp этого дополнения не указан.

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
| `/etc/os-release`, дополнение пользователя | **Ubuntu 24.04.5 LTS, noble** | Distro сообщён пользователем; больше не предполагается по shell prompt |
| `command -v cmake gcc g++ nvcc llama-cli llama-server` | Только **`/usr/bin/gcc`**, **`/usr/bin/g++`** | В данном PATH найдены gcc/g++; cmake, nvcc, llama-cli и llama-server не найдены этим вызовом, их отсутствие во всех каталогах не доказано. make/ninja/llama-mtmd-cli этим списком не проверялись |
| `df /mnt/c`, дополнение пользователя | **C: 953G size, 858G used, 96G avail, 90%** | Округлённые значения `df` для смонтированного C:, не exact bytes и не allocated размер VHDX. Пользователь сообщил об освобождении места; размер изменения не указан |

## Статусы и решения

- Target synthetic suite: **PASS по пользовательскому выводу**. Агент не повторял suite; 4.296 s — время синтетических тестов, не latency/tokens per second модели.
- GPU visibility и guest RAM/filesystem snapshot: **данные получены от пользователя**. Distro Ubuntu 24.04.5 LTS/noble и результат PATH-проверки записаны в дополнении. WSL version, kernel/architecture/libc, версии найденных инструментов, Windows build/free memory, exact host volume bytes и physical VHDX accounting ещё не записаны.
- llama.cpp CUDA inference, GGUF/projector pairing, TTFT/throughput, peak RAM/VRAM под моделью, code/RU/vision quality, offline isolation и cold restore: **not run / результатов нет**.
- Сохранить текущее окружение около 8 GB как первый baseline. Не требуется менять RAM сейчас. Возможное сравнение ceiling 8/10 GB остаётся поздним отдельным опытом, без правок memory/swap или shutdown в этом шаге.
- Ограничение всего продукта **100000000000 bytes** сохраняется. Guest `953G avail` и поздний C: `96G avail` не заменяют 100 GB product ledger: они округлены и не дают allocated VHDX/точный Windows free-byte учёт. Сообщение об освобождении места не меняет product cap.

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

Distro и указанную PATH-проверку повторять не требуется. В текущем **WSL shell** остаются read-only сведения для выбора совместимого runtime:

```sh
uname -m
getconf GNU_LIBC_VERSION
gcc --version
g++ --version
```

Отсутствие команды в PATH не доказывает отсутствие portable бинарника в другом каталоге. Неподдерживаемый `getconf` тоже сохранить как результат. Не вызывать cmake/nvcc как найденные инструменты и не устанавливать toolkit из-за одного PATH miss. Из каталога клонированного **local-ai-guru** отдельно записать:

```sh
git rev-parse HEAD
```

Org готовит отдельный ручной шаг закреплённого prebuilt CUDA 12.8 runtime вместо установки toolkit; загрузка/распаковка или запуск runtime этим inventory не подтверждены. Выбор совместимого артефакта и его точный состав передаёт Org после проверки оставшихся сведений. Сейчас **не выполнять** установку, загрузку runtime/моделей или конфигурацию WSL. Первый модельный shortlist уже выбран: Qwen3.5-9B и обязательное сравнение Gemma 4 E4B; новый survey не требуется. Точный preparation/download лот формируется после недостающих сведений и отдельного разрешённого шага.

План и gates: [PLAN.md](../../PLAN.md); модельный состав и byte evidence: [models.md](models.md), [offload.md](offload.md).
