# Авто-приёмка (SPEC.md, §22 «Приёмка перед релизом»;
# doc/LOAD-CRITERIA-QUESTIONS.md).
#
# Долгие проверки: полный объём, деградация, длительная работа, возврат памяти,
# рост файлов и базы, остановка. На своей машине — перед выпуском версии и
# после любой правки, касающейся хранения данных; в обычную сдачу и в CI
# не входят. Ожидаемое время и диагностика — qa/README.md, «Перед релизом».

.PHONY: acceptance

# Модули — каждый со своей копией стенда; регрессия идёт после них отдельным
# шагом: её артефакты должны быть уже собраны (как в test_load_regression.py).
acceptance:
	ACCEPTANCE=1 uv run pytest \
		qa/test_acceptance_volume.py \
		qa/test_acceptance_degradation.py \
		qa/test_acceptance_soak.py \
		qa/test_acceptance_memory.py \
		qa/test_acceptance_growth.py \
		qa/test_acceptance_shutdown.py -v
	ACCEPTANCE=1 uv run pytest qa/test_acceptance_regression.py -v
