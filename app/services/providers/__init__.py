"""
Video provider registry.

Yeni bir provider eklemek için sadece şunları yapman gerekir:
  1. providers/ altında yeni bir .py dosyası oluştur (VideoProvider'dan türet).
  2. Aşağıdaki PROVIDER_REGISTRY sözlüğüne ekle.
  Orkestrasyona (material.py) dokunmana gerek yok.
"""
from typing import Dict, List, Type

from .base import VideoProvider
from .pexels import PexelsProvider
from .pixabay import PixabayProvider
from .coverr import CoverrProvider
from .nasa import NASAProvider
from .wikimedia import WikimediaProvider
from .archive_org import ArchiveOrgProvider
from .utils import safe_error_details

# ─── Kayıt ───────────────────────────────────────────────────────────────────
PROVIDER_REGISTRY: Dict[str, Type[VideoProvider]] = {
    "pexels":       PexelsProvider,
    "pixabay":      PixabayProvider,
    "coverr":       CoverrProvider,
    "nasa":         NASAProvider,
    "wikimedia":    WikimediaProvider,
    "archive_org":  ArchiveOrgProvider,
}

# Skorlamada kullanılan kalite ağırlıkları (0.0–1.0)
PROVIDER_QUALITY_WEIGHTS: Dict[str, float] = {
    "pexels":       1.00,
    "pixabay":      0.95,
    "coverr":       0.90,
    "nasa":         0.75,
    "wikimedia":    0.70,
    "archive_org":  0.65,
}

# UI'da görünecek etiketler
PROVIDER_DISPLAY_NAMES: Dict[str, str] = {
    "pexels":       "Pexels",
    "pixabay":      "Pixabay",
    "coverr":       "Coverr",
    "nasa":         "NASA Image Library",
    "wikimedia":    "Wikimedia Commons",
    "archive_org":  "Internet Archive",
}

# API key gerektirmeyen provider'lar
FREE_PROVIDERS: List[str] = ["nasa", "wikimedia", "archive_org"]


def get_provider(name: str) -> VideoProvider:
    """Provider adından instance döndürür."""
    cls = PROVIDER_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Bilinmeyen provider: '{name}'. "
                         f"Geçerli seçenekler: {list(PROVIDER_REGISTRY.keys())}")
    return cls()


def get_active_providers(enabled_sources: List[str]) -> List[VideoProvider]:
    """
    Verilen isimler için provider instance'ları oluşturur.
    is_available() False dönen provider'lar (eksik API key vb.) atlanır.
    """
    from loguru import logger
    providers = []
    for name in enabled_sources:
        if name not in PROVIDER_REGISTRY:
            logger.warning(f"[providers] bilinmeyen kaynak atlandı: '{name}'")
            continue
        try:
            p = PROVIDER_REGISTRY[name]()
            if p.is_available():
                providers.append(p)
            else:
                logger.info(
                    f"[providers] '{name}' atlandı — "
                    "API key eksik veya provider kullanılamaz."
                )
        except Exception as e:
            logger.warning(
                f"[providers] '{name}' initialization failed: "
                f"{safe_error_details(e)}"
            )
    return providers
