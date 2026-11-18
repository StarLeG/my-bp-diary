"""Compose-файлы репозитория: YAML без дублей ключей (находка F11, 2026-11-18).

Дубль `CABINET_URL` в `qa/docker-compose.qa.yml` PyYAML молча съедал
(остаётся последний), но docker compose отклонял файл целиком
(«mapping key already defined») — стенд не пересобирался. Юнит-тест ловит
это без Docker и — в отличие от робота — работает даже когда сам
`qa/docker-compose.qa.yml` сломан (робот тогда не стартует). Тот же чек
в авто-приёмке — `TestUserFindings.test_f11_compose_yaml_has_no_duplicate_keys`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from qa.acceptance_common import COMPOSE_FILES, compose_files, load_yaml_strict

#: Корень репозитория (тест лежит в tests/).
ROOT = Path(__file__).resolve().parents[1]


class TestComposeYaml:
    def test_all_compose_files_present(self) -> None:
        """Все известные compose-файлы на месте — пропавший файл не пройдёт мимо."""
        missing = [str(rel) for rel in COMPOSE_FILES if not (ROOT / rel).exists()]
        assert not missing, f"нет compose-файлов: {missing}"

    def test_no_duplicate_keys(self) -> None:
        """Ни один compose-файл не содержит дублей ключей (F11)."""
        files = compose_files(ROOT)
        assert files, f"compose-файлы не найдены под {ROOT}"
        for path in files:
            load_yaml_strict(path)  # YAMLError с файлом и строкой на дубле

    def test_detector_flags_duplicate_key(self, tmp_path: Path) -> None:
        """Детектор действительно находит дубль (и не срабатывает на чистом файле)."""
        import yaml

        broken = tmp_path / "broken.yml"
        broken.write_text("services:\n  bot:\n    image: a\n    image: b\n", encoding="utf-8")
        with pytest.raises(yaml.YAMLError, match="дубль ключа"):
            load_yaml_strict(broken)

        clean = tmp_path / "clean.yml"
        clean.write_text("services:\n  bot:\n    image: a\n    ports: []\n", encoding="utf-8")
        assert load_yaml_strict(clean) == {"services": {"bot": {"image": "a", "ports": []}}}
