"""Abstract base class for all video providers."""
from abc import ABC, abstractmethod
from typing import List

from app.models.schema import MaterialInfo, VideoAspect


class VideoProvider(ABC):
    """
    Her video sağlayıcısının uygulaması gereken arayüz.

    Yeni bir kaynak eklemek için:
      1. Bu sınıftan türetin.
      2. `name`, `quality_weight` sınıf değişkenlerini doldurun.
      3. `search()` metodunu implemente edin.
      4. Gerekiyorsa `is_available()` override edin (API key kontrolü vb.).
      5. `providers/__init__.py` içindeki PROVIDER_REGISTRY'e ekleyin.
    """

    # Sağlayıcının dahili kimliği (config ve UI'da kullanılır)
    name: str = ""

    # 0.0–1.0 arası kalite ağırlığı; skorlamada kullanılır
    quality_weight: float = 0.80

    @abstractmethod
    def search(
        self,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
    ) -> List[MaterialInfo]:
        """
        Verilen anahtar kelime için video listesi döndürür.

        Args:
            search_term: Arama terimi
            minimum_duration: Saniye cinsinden minimum video süresi
            video_aspect: Hedef en-boy oranı (portrait / landscape)

        Returns:
            MaterialInfo listesi (boş liste de geçerlidir — exception fırlatma)
        """

    def is_available(self) -> bool:
        """
        Sağlayıcının kullanılabilir olup olmadığını döndürür.
        API key gerektiren sağlayıcılar bunu override etmelidir.
        """
        return True
