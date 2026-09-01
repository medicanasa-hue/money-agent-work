# v1.3.5 entegrasyonu — ilk uygulama paketi

Tarih: 31 Ağustos 2026. [Ana plan](upstream_1_3_5_student_pack_ai_plan_2026-08-31.md).

**Durum:** A güvenlik işleri ve B grubundaki öncelikli düzeltmeler yerel entegrasyon dalına uygulandı. Bu, bütün v1.3.5/main özelliklerinin taşındığı veya dağıtıma hazır bir sürüm olduğu anlamına gelmiyor. Sürüm etiketi bu nedenle değiştirilmedi. Docker doğrulaması, genel coverage eşiği ve aşağıdaki kalan işler açık.

## Korunan çalışma

- Ana klasör: `C:/A/money`; dalı `codex/upgrade-v1.3.1`, başlangıç commit'i `9c7cab0b7dae8b6756d4454638ac388586188464`.
- Entegrasyon klasörü: `C:/A/money-worktrees/integrate-upstream-v1.3.5`; dalı `codex/integrate-upstream-v1.3.5`.
- Yedek: `C:/Users/oozda/.codex/backups/money/20260831-040551-upstream-start`. Dosya kopyaları, binary Git diff, durum kaydı ve SHA-256 manifesti içeriyor.
- Önceden değiştirilmiş 28 izlenen dosya, plan ve branding dosyası olmak üzere 30 dosya yedek ve worktree kopyasında doğrulandı. Son kontrolde ana klasördeki bu 30 dosyanın hash'leri hâlâ aynı.
- Gerçek `config.toml`, medya arşivi, storage ve OpenMontage özel durumu ana klasörde bırakıldı; test ortamına kopyalanmadı. Bu alanlar için **tam veri yedeği alındığı iddia edilmiyor**. Canlı geçiş öncesinde ayrıca veri yedeği gerekiyor.
- Çalışma ayrı dalda, henüz commit edilmemiş durumda. Önceki kaydedilmemiş özelleştirmelerle yeni entegrasyon farkları birlikte görülebileceğinden, yalnız bu çalışmanın farkı ayrıca `.codex/integration/implementation-only.patch` dosyasına çıkarıldı. Commit, push, merge, deployment veya dış hesap etkinleştirmesi yapılmadı.

## Uygulanan davranışlar

| Plan işi | Sonuç |
|---|---|
| A1–A2 | Güvenli kopya, başlangıç test/coverage kaydı ve sentetik medya referansı hazır. |
| A3 | `/tasks` dış symlink takibini açmıyor. Dosya erişimi API ile aynı anahtar kontrolünü ve başarısız deneme sınırını kullanıyor. |
| A4 | `x-task-id` için 128 karakter ve yazdırılabilir metin kontrolü; bozuk kimlik yerine UUID. Duplicate API key header reddi, Unicode güvenli karşılaştırma, yanlış config tipinde kapalı davranış. |
| A5 | Yeni materyal servisi: parça parça okuma, video için 200 MiB ve görsel için 20 MiB üst sınırı, UUID saklama adı, başarısızlıkta geçici dosya temizliği, JPEG/PNG içerik kontrolü ve FFmpeg ile video doğrulama. Geçici dosya/dizinler listelenmiyor. |
| A6 | HTTP özel ses yolu yalnız mevcut `storage/tasks` içindeki dosyalara çözümleniyor. Önceki görevden ses yeni bir görevde kullanılabiliyor; dış dosyalar ve dışarı çözümlenen yollar reddediliyor. Güvenilen yerel CLI/WebUI dosya seçimi korunuyor. |
| A7 | Bozuk JSON, eski/geçersiz şema, eksik veya beklenmeyen çağrı argümanları kuyruktan atlanıyor; mevcut görev başarısız işaretleniyor. Silinmiş görev yeniden yaratılmıyor. Schedule hatasında yeni state geri alınıyor. Eski positional kayıtlar yeniden kuyruğa yazılabilir biçime dönüştürülüyor. |
| B1 / B3b | Üretilen son ses dosyasının gerçek süresi öncelikli; ölçülemezse sağlayıcı zamanlaması kullanılıyor. ElevenLabs/Chatterbox/SiliconFlow clip handle'ları hata durumunda da kapanıyor. SiliconFlow son cue'su tam ses sonuna ulaşıyor. |
| B1b | ElevenLabs hata gövdesi sınırlı okunuyor, hesap kontrolü yanıtı kapatılıyor, proxy çıktısının büyüklüğü FFmpeg aşamasında da sınırlandırılıyor. |
| B2 | Ayrı parantezli açıklamalar arasındaki anlatımı silen greedy regex düzeltildi. |
| B3 | Font ascent/descent değerleri, mevcut satır sonları ve satır başına stroke boşluğu hesaba katılıyor; çok satırlı altyazı kesilmiyor. |
| B4 | Windows CLI UTF-8 çıkışı; Türkçe/Unicode ve boşluk içeren indirme adları için FileResponse header üretimi. |
| B5a tamam / B5b ertelendi | Tekrarlı BOM okuma ve EBUSY bind-mount kaydetme yolu testli. Mevcut config bölümleri/anahtar kaydı korunuyor. Upstream `01def3b` içindeki bütün ElevenLabs UI yeniden başlatma davranışları taşınmış sayılmıyor; kullanıcı dışarıda ürettiği sesi dosya olarak yüklediğinden bu sağlayıcı işi öncelikten çıkarıldı. |
| B6 — kısmi | FFmpeg yoksa medya üretimi senaryo/TTS/müzik çağrılarından önce duruyor; script/terms aşamaları bağımsız çalışıyor. Docker paket kurulum hatası artık başarı gibi sonuçlanmıyor; gerçek image build henüz doğrulanamadı. |
| B7 | 30 sesli Gemini kataloğu ve stil etiketleri; eski kaydedilmiş etiketlerle dispatch uyumu. Whisper için isteğe bağlı `initial_prompt`, Türkçe dil ipucuyla birlikte gönderiliyor. |
| C6 — başlangıç | Windows CI da pytest ile hem fonksiyon hem unittest testlerini topluyor. Canlı provider testleri/model indirmeleri CI varsayılanında kapalı. Sentetik render testi koleksiyona eklendi. |

Uygulanan başlıca upstream referansları: `9f30509`, `28a55dd`, `5754ff9`, `7003aba`, `4a82f8c`, `93c0365`, `17b7b3f`, `254cd02`, `b4c2810`, `0e9e64a`, `3a96206`, `8e1add3`, `8cf6726`, `a41d7cb`, `fe29211`, `85d321d`, `e5bb283`, `1339ee8`, `9faef69`, `fec2721`, `89fe953`, `2588821`. Dosyalar topluca üstüne yazılmadı; yerel modül ayrımları ve mevcut özellikler korundu.

## Uyumluluk notları

1. API anahtarı ayarlıysa `/tasks` dosyaları localhost üzerinde de anahtar ister. Anahtarsız çıplak medya URL'si kullanan istemciler header göndermeli. Anahtar boşsa mevcut yerel erişim davranışı korunur; bu durum çok kullanıcılı güvenli hosting anlamına gelmez.
2. API özel ses örneği: `custom_audio_file="onceki-gorev-id/audio.mp3"`. Bu dosya tasks kökünde bulunmalıdır. Sunucunun herhangi bir disk yolunu API'ye vermek artık kabul edilmez. Bu sınır, kullanıcı bazlı erişim ayrımı değildir; mevcut tek kullanıcı/paylaşılan anahtar modelini korur.
3. Materyal yüklemesinin döndürdüğü UUID dosya adı kullanılmalıdır. Aynı özgün adla yeniden yükleme önceki dosyanın üzerine yazmaz. Boyut kontrolü servis aşamasındadır; public reverse proxy üzerinde ayrıca request-body sınırı gerekir.
4. FFmpeg video doğrulaması yalnız yerel dosya protokolü ve video demuxer listesi kullanır; video uzantısıyla gönderilen playlist reddedilir. Bu bir genel medya sandbox'ı değildir; public kullanımda kaynak kotaları/worker izolasyonu ayrıca gerekir.
5. Request ID log/izleme bilgisidir, gerçek görev dizininin kimliği değildir. Ana plandaki “path-safe ID” ifadesi bu uygulamada “log için güvenli request ID” olarak netleştirildi.
6. Bind mount EBUSY durumunda atomik rename mümkün olmadığından kilit altında yerinde yazma kullanılır. Diğer dosya sistemi hataları yutulmaz. Süreç/elektrik kesintisine karşı bu fallback atomik yazma garantisi vermez.

## Doğrulama

| Kontrol | Sonuç |
|---|---|
| Başlangıç tam test | 1.257 geçti, 2 TwelveLabs testi başarısız, 10 atlandı. |
| İlk entegrasyon paketi tam test | **1.346 geçti, 13 atlandı; 12.162 subtest geçti.** Süre yaklaşık 124 saniye. Sonraki test paketi ayrıca aşağıda kaydedildi. |
| Başlangıç toplam coverage | %67,77. Başlangıç verisi kendi kaynak snapshot'ına eşlenerek hesaplandı. |
| İlk entegrasyon paketi toplam coverage | **%68,70**; o aşamada yapılandırılmış %70 eşiği başarısızdı. Eşik değiştirilmedi. |
| Entegrasyon farkındaki kod | 330 yeni/değişen yürütülebilir satırın 310'u kapsandı: %93,94. İlgili branch'ler dahil hesap %93,17. Bu, tüm modüllerin veya tüm projenin %93 olduğu anlamına gelmez. |
| Lint / compile / diff | Ruff, Python compileall ve diff whitespace kontrolü geçti. |
| Bağımlılık taraması | İlk tarama 3 pakette 11 bildirim; son tarama **121 pakette 0 bilinen açık**. Bu sayı mutlak güvenlik garantisi değildir. |
| Bağımsız inceleme | Auth, dosya sınırı, özel ses, upload ve Redis kapsamında açık P1/P2 bulgusu kalmadı. Dört inceleme bulgusu RED→GREEN testlerle kapatıldı. |
| Gerçek medya | 1080×1080, 2,02 saniye, ses ve Türkçe çok satırlı altyazı. FFmpeg tam decode ve materyal yükleme doğrulaması geçti. |

Başlangıçta başarısız olan TwelveLabs testleri optional SDK'nın input tiplerini mocklamıyordu. Üretim bağımlılığını zorunlu yapmadan, testlerde dar input tipleri ve gönderilen alanların assertion'ları eklendi. Hata skip ile gizlenmedi.

Yeni regresyonlar önce başarısız çalıştırıldı; kanıt logları `.codex/integration/*-red.log` ve odaklı green loglarında. Tam log `final-test.log`; coverage ve fark özeti `final-coverage.json` / `change-summary.json`. Üç uyarı mevcut bağımlılık/test davranışlarından geliyor: Starlette test client deprecation, pydub/audioop deprecation ve sahte ses fixture'ındaki MoviePy finalizer uyarısı.

Windows'taki 13 atlamanın üçü symlink oluşturma yetkisinin olmamasından kaynaklanıyor; diğerleri başlangıçta da atlanan opt-in testler. Symlink kontrolleri Linux CI'da doğrulanmalı. Gerçek Redis sunuculu yeniden başlatma/lease/ACK testi bu pakette yapılmadı.

## Medya karşılaştırması

Yedekteki `video.py` ve güncel kod aynı sentetik video, ses, font ve SRT ile çalıştırıldı; gerçek kullanıcı medyası kullanılmadı. Eski kodda ilk/son altyazı satırları kesiliyordu; güncel karede bütün metin görünür. Bu kısa örnek performans kıyaslaması veya bütün TTS sağlayıcıları için uçtan uca onay değildir.

- Güncel video: `.codex/integration/render-smoke/turkce-smoke.mp4`
- Önceki kodla video: `.codex/integration/render-smoke/baseline.mp4`
- Kareler: `preview.png`, `baseline-preview.png`
- Ölçümler: aynı klasörde `result.json`, `comparison.json`
- CI için tekrarlanabilir test: `test/services/test_render_smoke.py` — sentetik 1 saniyelik varyant.

## Güvenlik bağımlılıkları

Yalnız üç sürüm değişti: aiohttp 3.14.1 → **3.14.3**, cryptography 49.0.0 → **50.0.1**, GitPython 3.1.55 → **3.1.58**. `pyproject.toml`, legacy `requirements.txt` ve `uv.lock` birlikte güncellendi. Mevcut Pillow 12.3.0 override'ı, multipart ve diğer ana sürümler korundu.

Aiohttp istemci kodu Edge TTS üzerinden kullanılıyor. PKCS#7 decrypt veya saldırgan kontrollü GitPython kwargs yolu uygulama kodunda saptanmadı; 11 bildirim 11 erişilebilir saldırı yolu olarak yorumlanmadı. Sürüm seçimleri [aiohttp değişiklik kaydı](https://docs.aiohttp.org/en/stable/changes.html), [cryptography değişiklik kaydı](https://cryptography.io/en/latest/changelog/) ve [GitPython 3.1.58 sürümü](https://github.com/gitpython-developers/GitPython/releases/tag/3.1.58) üzerinden doğrulandı.

## Sonraki uygulama sırası

0. Geliştirme araçlarını entegrasyon sırasında kullan: Copilot için repo
   talimatları ve CodeQL workflow'u yerelde eklendi; CI'ın push filtresine
   GitHub'daki gerçek varsayılan dal `codex/upgrade-v1.3.1` eklendi.
   Copilot hesabı/istemci oturumu doğrulandı; ilk küçük test görevi tamamlandı.
   Secret protection ve Dependabot ayarları ayrıca verilen kullanıcı onayıyla
   açıldı; bu paketin uzak CI/CodeQL çalışması henüz yok. Ayrıntılı kullanım
   ve görev şablonu ana planın 5.
   bölümünde. Bu işler C aşaması sonuna ertelenmiyor.

1. Yerel genel coverage açığı ikinci test paketinde kapatıldı: %70,89 ve %70 kapısı başarılı. %80 proje hedefi henüz tamamlanmadı. Kalan atlamaları, Linux/Python 3.13/gerçek Redis CI sonuçlarını doğrula. Yeni kritik kodun yüksek kapsamı, bütün depo eşiğinin yerine geçmez.
2. B1c: Harici anlatım dosyasının mevcut WebUI → görev → altyazı/render akışını doğrula. TTS atlama, gerçek süre, Türkçe altyazı ve bozuk dosya davranışlarını önceliklendir. ElevenLabs'a özel UI/API kalıcılığı B5b ertelendi; API kurulumu gerekmiyor.
3. Docker motoru kullanılabilir olduğunda B6b/C5 yerel build, kendi imajı ve rollback doğrulamasını tamamla. Legacy pip/Docker kurulumu uv'nin Pillow override'ını otomatik devralmıyor; dağıtım öncesi kurulum yollarını tek kilitli kaynağa yaklaştır ve imajı ayrıca tara. `.dockerignore` bu turda agent/diagnostic dosyalarını ve worktree `.git` dosyasını dışarıda bırakacak şekilde sıkılaştırıldı.
4. C1 arama cache'i, C2 provenance/lisans manifesti, C3 preset kalıcılığı ve C4 sosyal açıklama davranışlarını küçük ayrı paketler halinde taşı.
5. C aşaması doğrulandıktan sonra öğrenci avantajlarıyla Sentry/Doppler/CodeScene ve sınırlı bulut denemeleri; D aşamasında kullanım/maliyet ölçümü, bütçe kontrolü ve storyboard düzenleme.

İlk entegrasyon paketinde Copilot Student, Gemini/AI Pro, Flow, Azure, Sentry veya başka öğrenci avantajı etkinleştirilmedi. Sonraki Copilot bağlantısı ve onaylı güvenlik ayarları aşağıda ayrı kaydedildi. Ücretli API deneyi, uzaktan ajan görevlendirmesi ve yayın yapılmadı.

31 Ağustos ilk ek kontrolü: GitHub'da CodeQL default setup `not-configured`,
Dependabot security updates, secret scanning ve push protection kapalıydı.
O aşamada yalnız durumları okundu; onaylı etkinleştirme son bölümde kaydedildi.
GitHub geliştirme araçlarının yapılandırılması ile
hesapta etkinleşip gerçekten çalışması ayrı kabul ölçütleridir. Mevcut Docker
publish workflow'u upstream namespace'e işaret ettiğinden main/tag yayını
öncesinde ayrıca düzeltilmeli; bu turda otomatik yayın tetiklenmedi.

Bu küçük ek pakette YAML ayrıştırma, PR/default branch tetikleyicileri, token
izinleri, gerçek commit SHA referansları ve whitespace kontrol edildi.
Copilot talimatları 3.495 karakter. Bağımsız incelemede yeni kapsamda açık
P1/P2 bulunmadı. Üretim Python kodu değişmediğinden tam test paketi tekrar
çalıştırılmadı; önceki test/coverage sonuçları yukarıdaki ilk pakete aittir.
Orijinal çalışma alanındaki 30 dosyanın hash kontrolü tekrar geçti.

## Copilot yerel kurulum takibi — 31 Ağustos

- Resmi WinGet `GitHub.Copilot` paketi kullanıcı kapsamına kuruldu: **1.0.82**.
  İndirme hash'i WinGet tarafından doğrulandı; `--version` ve `login --help`
  çalıştı. Kullanıcı PATH'ine paket klasörü eklendi; açık terminaller yeni
  PATH için yeniden açılmalı. Node 20 kurulumu değiştirilmedi. Mevcut Codex
  oturumunda PowerShell 7.6.4 bulunduğundan bağımlılık kurulumu atlandı;
  sistem geneline ayrıca PowerShell kurulmadı.
- Gerçek exe yolu:
  `C:\Users\oozda\AppData\Local\Microsoft\WinGet\Packages\GitHub.Copilot_Microsoft.Winget.Source_8wekyb3d8bbwe\copilot.exe`.
  WinGet Links dizininde exe alias'ı yok; paket dizini PATH üzerinden kullanılıyor.
- VS Code **1.128.1** içinde Copilot Chat **0.56.0** yerleşik. Normal
  `--list-extensions` listesinde görünmüyor. Chat kurulum komutu mevcut
  bileşeni doğruladı; eski `GitHub.copilot` paketi ise Chat'i 0.48.1'e
  indirmeyi denediğinden VS Code bunu reddetti. Force/downgrade uygulanmadı;
  mevcut Continue ve Roo eklentileri korundu.
- Kullanıcının paylaştığı tarayıcı ekranı farklı öğrenci hesabında **Copilot
  Student aktif**, **0/200 AI kredisi**, **ücretli ek kullanım kapalı** durumunu
  gösteriyor. Kullanıcı `medicanasa-hue` hesabının öğrenci hesabı olmadığını
  açıkça belirtti; iki hesabın hakları birbirine eşitlenemez.
- Boş geçici klasörde Copilot açıldı; uygulama kaynakları yüklenmedi,
  custom instructions/model araçları/built-in MCP/uzak paylaşım kapalı tutuldu.
  Yalnız bu boş klasör için oturumluk güven verildi, kalıcı klasör güveni yok.
  `/user`: **medicanasa-hue (via gh)**; `/usage`: sıfır mesaj, **0 AI kredisi**,
  **0/200 AIC**. Bu ölçüm öğrenci olmayan `medicanasa-hue` hesabına aittir;
  tek başına Student hakkını doğrulamaz. Oturum `/exit` ile kapatıldı.
- İlk kontrolde CLI mevcut gh girişini kullandı. Hesap ayrımı netleşince
  `copilot login --web-flow` başlatıldı; kullanıcı tarayıcıda onayladı.
  Sonuç: **Signed in successfully as yusufyigitozdamar**, exit code 0.
- İkinci ayrı CLI kontrolünde `/user`: **yusufyigitozdamar**; `/usage`:
  sıfır mesaj, **0 AI kredisi**, **0/200 AIC**. Aynı boş geçici klasör ve
  kapalı model araçları/MCP/uzak paylaşım kullanıldı; kodlama isteği yok.
  `gh api user` hâlâ **medicanasa-hue** gösterdi. Kullanıcı gerekirse gh
  hesabının da değişmesine izin verdi; ayrı Copilot girişi çalıştığından
  mevcut depo hesabı değiştirilmedi.
- CLI kurulumu ve seçilen hesaba erişim tamam. Kullanıcı editörde kendisi
  kullanmak isterse VS Code'da öğrenci hesabıyla giriş yapabilir;
  editör oturumu doğrulanmadı. Kurulum aşamasında model isteği gönderilmedi;
  ilk kodlama kullanımı aşağıdaki ayrı test paketinde kaydedildi.

## Onaylı GitHub güvenlik ayarları — 31 Ağustos

Kullanıcı yalnız Secret scanning, repo push protection ve Dependabot güvenlik
özelliklerini açmayı onayladı. İşlemler `medicanasa-hue/money-agent-work`
deposunda, yönetici yetkisi doğrulanan mevcut gh hesabıyla yapıldı:

| Özellik | İşlem | Son bağımsız GET kontrolü |
|---|---|---|
| Secret scanning | Repository PATCH, yalnız ilgili status alanı | `enabled` |
| Repo push protection | Repository PATCH, yalnız ilgili status alanı | `enabled` |
| Dependabot alerts / dependency graph | `PUT .../vulnerability-alerts` | GET HTTP 204 |
| Dependabot security updates | `PUT .../automated-security-fixes` | `enabled: true, paused: false` |

Dependency graph, resmi vulnerability-alerts endpointinin parçası olarak
etkinleşti; ayrı bir hesap veya hizmet kurulmadı. Güncelleme özelliği PR
oluşturabilir, ancak **allow_auto_merge false** kaldı. Bu kayıt, taramanın
tamamlandığını veya depoda hiç güvenlik açığı bulunmadığını göstermez.

Öncesinde ve sonrasında varsayılan uzak dal aynı commit'teydi:
`c98fff2c1b4c9aafadb7598856863a84f97580fa`. Push, merge, yayın, manuel Actions
dispatch veya ücretli servis etkinleştirmesi yapılmadı. CodeQL default setup
`not-configured` olarak kaldı; advanced workflow yalnız yerelde hazır.
Secret validity checks ve non-provider pattern ayarları değiştirilmedi.

API davranışı [GitHub REST dokümanından](https://docs.github.com/en/rest/repos/repos#enable-vulnerability-alerts)
doğrulandı. Özelliklerin GET kontrolleri dışında uygulama testleri tekrar
çalıştırılmadı; bu adım üretim Python kodunu değiştirmedi.

## İkinci test paketi: Copilot kullanımı ve %70 kapsam kapısı

Kullanıcının devam isteğiyle, coverage açığı üç davranış grubuna bölündü.
Değişiklikler aynı ayrı worktree'de yapıldı; bu turda üretim Python kodu,
bağımlılıklar ve coverage yapılandırması değiştirilmedi. Yeni test atlaması
veya coverage dışlama kuralı eklenmedi.

Son teslim kontrolünde genel `test_*.py` gitignore kuralının yeni kalite testini
gizlediği görüldü. Yalnız `test/utils/test_video_quality.py` için istisna
eklendi; test dosyası artık Git değişiklik listesi ve entegrasyon patch'inde
yer alıyor. Bu, coverage kapsamını değiştiren bir kural değildir.

| Dosya | Doğrulanan davranışlar | Yeni test |
|---|---|---:|
| `test/services/test_video_pipeline_edges.py` | Altyazı yazarken eski çıktı/yarım dosya temizliği; GPU hatasında CPU; crossfade zamanlaması; filtre desteği; odaklı kırpma; ses karıştırma sözleşmeleri | 22 |
| `test/services/test_task_pipeline_edges.py` | Kısmi/tam render hatası; yayın onayı; materyal/provenance; bozuk checkpoint; ses/altyazı kurtarma; karaoke varyantı; kalite ölçümü hata verse de tamamlanan çıktı korunması | 28 |
| `test/utils/test_video_quality.py` | FFmpeg yok/hatalı/timeout ile geçerli boş sonuç ayrımı; sahne/karanlık/donmuş aralık yorumlama; bozuk/sonsuz değerler; Unicode dosya yolu; süre/eşik sınırları | 44 |

Copilot yalnız son dosyanın taslağını üretti; diğer iki dosya yerel Codex
ajanlarıyla hazırlandı. Taslaktaki donmuş aralık beklentisi yanlıştı: verilen
olaylarda açık bir bitiş zamanı varken start+duration sonucu bekleniyordu.
Eksik/geçersiz bitiş senaryosu doğru kuruldu; doğru üretim davranışı
değiştirilmedi. Beklenmeyen subprocess çağrısını durduran fixture ve ek sınır
assertion'ları eklendi. Bu bir üretim hata düzeltmesi değil, mevcut davranışın
regresyon testleriyle kaydıdır.

### Copilot koşusu ve tüketim

- CLI **1.0.82**, daha önce bağlanan `yusufyigitozdamar` hesabı; GitHub CLI
  depo hesabı değiştirilmedi. **Auto**, bu istekte `mai-code-1.1-flash` seçti.
- Boş geçici klasörden yalnız `app/utils/video_quality.py` kaynağı ve dar görev
  metni gönderildi. Model araçları, built-in MCP, custom instructions, uzak
  paylaşım ve otomatik güncelleme kapalıydı. Özel config, anahtar ve kullanıcı
  medyası prompt'a eklenmedi. Bu bir Copilot cloud agent görevi değildi.
- Tek başarılı model isteği: **18.990 giriş**, **9.428 çıkış** tokenı;
  7.424 reasoning tokenı çıkış toplamına yeniden eklenmez.
  `totalNanoAiu=1360044000`, yaklaşık **1,36 AI kredi**; görüntülenen
  200 kredilik hakkın %0,68'i. Bu koşu hesabın yeni kalan-bakiye sorgusu veya
  fatura doğrulaması değildir. `totalPremiumRequestCost=1` ayrı alanıdır.
- CLI `--max-ai-credits` için en az 30 kabul etti; bu oturum limiti tüketilen
  miktar değildir, tek istek nedeniyle aşılabilecek yumuşak sınırdır. Auto ile
  `--effort` kullanılamadı; sonraki çalıştırmada model/effort zorlanmadı.
  Önceki argüman doğrulama hataları model isteği üretmedi.
- CLI dosya değiştirmedi. Taslak incelendikten sonra worktree'ye alındı;
  `.github/copilot-instructions.md` otomatik yüklenmediği için ilgili
  sınırlar görev metninde açıkça verildi.

Birim yorumu [resmi SDK kullanım metrikleri](https://github.com/github/copilot-sdk/blob/main/docs/features/usage-and-billing.md#accumulated-ai-credit-and-token-totals)
ve [model ücretlendirme tablosuyla](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing)
karşılaştırıldı. Kullanım raporu: `.codex/integration/copilot-quality-usage.json`;
prompt ve ilk yanıt aynı klasörde `copilot-quality-prompt.txt` /
`copilot-quality-response.md`. Bunlar ignored yerel tanılama dosyalarıdır.

### Son doğrulama

| Ölçüm | Önce | Sonra |
|---|---:|---:|
| Tam pytest | 1.346 geçti / 13 atlandı | **1.440 geçti / 13 atlandı** |
| Subtest | 12.162 | 12.162 |
| Branch dahil toplam coverage | %68,7031 | **%70,8894** |
| Kapsanan yürütülebilir satır | 16.857 / 23.385 | 17.366 / 23.385 |
| Kapsanan branch | 4.858 / 8.222 | 5.040 / 8.222 |
| Mevcut %70 coverage kapısı | Başarısız | **Başarılı, exit 0** |
| `task.py` branch dahil coverage | %67,92 | %83,89 |
| `video.py` branch dahil coverage | %48,41 | %60,49 |
| `video_quality.py` branch dahil coverage | %38,18 | %100 |

Tam paket 124,37 saniye sürdü. Önceki üç uyarı ve 13 atlama aynı kaldı;
bu turda yeni atlama eklenmedi. Ruff, compileall, yeni dosyaların format
kontrolü ve diff whitespace kontrolü başarılı. Tam paket mevcut sentetik
FFmpeg render testini de içeriyor; subprocess mock testleri gerçek GPU veya
bütün medya çeşitleri için görsel kalite garantisi değildir.

Yeni 94 test birlikte geçti. Bağımsız incelemede açık P1/P2 bulunmadı;
P3 odak noktası assertion'ı güçlendirildi ve ilgili 22 test tekrar geçti.
Copilot testleri üç ayrı süreçte yalnız bellekte oluşturulan kasıtlı
bozulmaları yakaladı: sahne zamanlarının kaybı, ölçüm hatasının boş liste
olarak dönmesi, donmuş aralık başlangıcının negatife düşmesi. Her koşuda
beklenen test hatası görüldü (sırasıyla 2, 3, 1 test); üretim dosyası diskte
hiç değiştirilmedi. Son tam paket normal üretim koduyla başarılıdır.

Tekrar üretme: ayrı worktree'de `MPT_RUN_INTEGRATION_TESTS=0`,
`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` ile
`uv run --no-sync python -X utf8 -m coverage run -m pytest -q test`, ardından
`uv run --no-sync python -X utf8 -m coverage report --precision=2`.
Bu koşuda `COVERAGE_FILE=.codex/integration/coverage-round2.coverage` kullanıldı.
Kanıtlar: `coverage-round2-tests.log`, `coverage-round2-report.txt`,
`coverage-round2.json`, `mutation-*.log`.

İzolasyon sınırı: config ve kalıcı görev durumu fixture'lardan önce import
sırasında yüklenebiliyor. Offline bayrakları bu disk işlemlerini kapatmıyor.
Koşu, özel config/storage taşınmadan hazırlanmış ayrı worktree'de yapıldı;
modül/dizin yollarının orijinal `C:\A\money` alanına bağlanmadığı ayrıca
kontrol edildi. Genel import öncesi test izolasyonu ileriki test altyapısı
işidir; testler gerçek üretim storage'ı üzerinde çalıştırılmamalı.

Bu paket yerel %70 kapısını kapatır; proje genelindeki %80 hedefini, Linux
ve Python 3.13 doğrulamasını veya gerçek Redis CI'sını tamamlanmış saymaz.
Kullanıcının son düzeltmesiyle sıradaki yerel iş harici ses dosyası akışının
doğrulanması; sonrasında Docker/uv kurulum tutarlılığıdır. ElevenLabs'a özel
UI/API kalıcılığı öncelikten çıkarıldı. Push, commit, merge,
yayın, bulut kaynağı veya ücretli API servisi etkinleştirmesi yapılmadı.

## Kullanıcı düzeltmesi: ElevenLabs dışarıda, MPT'de ses dosyası

Kullanıcı ücretli ElevenLabs API kullanmadığını, genellikle ücretsiz kredilerle
ürettiği sesi MPT'ye dosya olarak eklediğini açıkladı. Önceki sonraki-adım
önerisi buna göre düzeltildi; yeni abonelik, API anahtarı veya sağlayıcı ayarı
gerekmiyor. Var olan TTS seçenekleri ve tamamlanmış kaynak/güvenlik düzeltmeleri
korunacak; genel config/preset işleri sağlayıcıya özel B5b'ye bağlanmayacak.

Kaynak incelemesinde bu akış zaten mevcut: WebUI dosyayı görevin
`custom-audio.<uzantı>` yoluna kaydedip `custom_audio_file` parametresine veriyor;
`task.generate_audio` özel dosya varsa `voice.tts` çağırmadan gerçek ses süresini
kullanıyor. Altyazıda ses zamanlaması yoksa mevcut Whisper yolu ve gerektiğinde
senaryo süreleme fallback'i var. Fallback'in çalışması hassas kelime senkronu
garantisi değildir; öncelikli kontrol gerçek Türkçe ses/altyazı uyumudur.

Özgün ses metni `video_script` alanına girilirse korunuyor; alan boşsa konuya
göre yeni senaryo üretilebiliyor. Dolayısıyla yüklenen sesin otomatik senaryo
kaynağı olduğu varsayılmamalı. `_load_resume_media` yalnız standart ses
adlarını aradığından `custom-audio.*`, kesirli süre ve altyazı korunması için
odaklı kurtarma kabul testi de B1c'ye eklendi; bu gözlem bu turda kanıtlanmış
yeni bir üretim hatası veya uygulanmış düzeltme olarak sunulmuyor.

Yayın öncesi ayrı lisans kontrolü: ElevenLabs'ın güncel belgesi Free plan
çıktılarına ticari lisans vermiyor ve ticari olmayan paylaşımda atıf istiyor.
Kullanıcının "ücretsiz kredi" ifadesi hesap planının bağımsız doğrulaması
değildir; promosyon/özel plan varsa kendi koşulu incelenmeli. Teknik olarak
dosya yükleyebilmek yayın hakkı vermez. [Resmi ElevenLabs koşulları](https://elevenlabs.io/docs/help-center/legal/can-i-publish-the-content-i-generate-on-the-platform).

Bu tercih kaydı aşamasında yalnız plan/kayıt güncellendi ve kaynaklar incelendi;
yeni ses üretimi, API çağrısı veya üretim kodu değişikliği yapılmadı. Sonraki
uygulama ve doğrulama aşağıdaki ayrı kayıttadır. Hiçbiri canlı ElevenLabs
doğrulaması olarak yorumlanmamalı.

## Üçüncü paket: harici ses, altyazı uyarısı ve kesinti kurtarma

Kullanıcının devam isteğiyle B1c, aynı ayrı entegrasyon worktree'sinde uygulandı.
Ücretli ElevenLabs API hesabı, yeni sağlayıcı anahtarı veya ek abonelik gerekmedi.
Mevcut özel ses/TTS ayrımı korundu; aşağıdaki kanıtlanmış eksikler giderildi:

- Eksik, okunamayan, sıfır/negatif veya sonlu olmayan süreye sahip özel ses,
  senaryo sağlayıcısına gidilmeden reddediliyor. Normalizasyondan sonra süre
  yeniden doğrulanıyor. Hata, yeniden dosya yüklemeyi öneriyor; dosyanın özel
  yolunu göstermiyor. Mevcut genel hata alanındaki metin İngilizce kaldı.
- Kesilmiş görevde kayıtlı `custom_audio_file` seçimi kullanılıyor. Eski
  `audio.mp3` veya başka bir denemeden kalan normalize edilmiş ses bu seçimin
  yerini alamıyor. Normalizasyon mevcut ayarlarda açıksa seçilen özgün kaynaktan
  tekrar yapılıyor; kapalıysa özgün ses kullanılıyor. Bu, ayarın görev başına
  yeni bir geçmişini tutan değişiklik değildir. Kesirli süre ile düzenlenmiş
  SRT/ASS korunuyor; seçili ses geçersizse görev kesinti durumunda kalıyor.
- Whisper kullanılamayıp senaryo süresinden altyazı üretilirse
  `subtitle.review.json` içine `timing_source: script_estimate` kaydediliyor.
  Şüpheli metin satırı bulunmasa da uyarı görünüyor. Tekli tamamlanma ekranı,
  toplu sonuç ayrıntısı ve görev geçmişi aynı uyarıyı kullanıyor; boş tablo yok.
  Türkçe dahil dokuz mevcut dil sözlüğüne yalnız bu mesaj eklendi.
- Yeniden denenen Whisper çağrısı mevcut SRT'yi yeni transkript sanamıyor.
  Yeni çıktı geçici dizinde doğrulandıktan sonra asıl dosyaya geçiriliyor.
  Yeni çıktı yoksa elle düzeltilmiş SRT/ASS ve inceleme raporu korunuyor.
  Gerçek yeni transkript, yaklaşık zamanlama kaydını güncelliyor. Senaryodan
  tahmini altyazı üretirken eski karaoke kelime zamanları kullanılmıyor.

Hatalar önce başarısız testlerle gösterildi: süre/fallback paketi 14, tekrarlı
Whisper paketi 2, UI paketi 16 başarısız test üretti. Bunlar ayrı hedefli RED
koşularıdır; yeni testlerin toplamı veya tüm deponun başlangıç sonucu değildir.
Kurtarma, ses doğrulama, gerçek FFmpeg ve UI için dört odaklı test modülü eklendi.
UI testleri mevcut geçmiş/i18n kontrollerinin yanında iki küçük gerçek
Streamlit `AppTest` çalıştırmasını içeriyor. Bunlar tam tarayıcıyla yüklemeden
yayına kadar bütün ekranların E2E testi olarak sunulmuyor.

### Ücretli servis olmadan gerçek ses ve altyazı denemesi

WAV/MP3 × normalizasyon açık/kapalı dört otomatik test, yerelde üretilen 1,37
saniyelik sesi gerçek FFmpeg ile videoya dönüştürdü. Kaynak dosyanın değişmemesi,
çözülen sesin süresi, son bölümdeki farklı frekanslı işaret ve Türkçe altyazının
beklenen karelerde görünmesi doğrulandı. Bu testler dış TTS/LLM veya Whisper
modeli istemiyor; rutin CI içinde tekrar üretilebilir.

Buna ek olarak, Windows'ta zaten kurulu Microsoft Tolga sesiyle yalnız sentetik
bir Türkçe kayıt üretildi. Kullanıcının ElevenLabs dosyaları okunmadı. Ses:
“Bu bir ses yükleme denemesidir. İstanbul'da güneş doğuyor. Çocuklar gülerken
kuşlar ötüyor. Son kelime tamam.”

| Yerel deney | Sonuç |
|---|---|
| Kaynak PCM süresi | 8,87294 saniye |
| Kullanılan ASR | Önbellekteki `faster-whisper-base`, CPU/int8, 2 thread |
| Ağ/model indirme | Yok; açık yerel model yolu ve `local_files_only=True` |
| Senaryo ipucu | Kullanılmadı; dil `tr` olarak verildi |
| Model yükleme / altyazı üretimi | 0,58 / 1,60 saniye; yalnız bu makine ve örnek |
| Ham tanıma | Bu kısa örnekte sözcük farkı bulunmadı |
| Son “tamam” sözcüğü | 7,60–7,90 saniyede tanındı |
| Video / çözülen çıktı sesi | 9 / 8,87004 saniye; MP4 decode kontrolü başarılı |
| Son sözcük aralığında kaynak/çıktı PCM korelasyonu | 0,999982 |

Ham ASR metni ve kelime zamanları, senaryoyla düzeltmeden **önce** ayrı saklandı;
sonradan yalnız cümle sonu noktaları eklendi. Ham ASR altyazısıyla ayrıca gerçek
video üretildi ve son altyazı karesi incelendi. Bu tek kısa sentetik örnek;
uzun kayıt, müzik, farklı konuşmacı, ElevenLabs çıktısı veya diğer Whisper
modelleri için doğruluk/performans garantisi değildir. Model zamanları yaklaşık
tahmindir; kusursuz kelime senkronu iddiası yoktur. Büyük model yüklenmedi ve
kullanıcının seçili Whisper ayarı değiştirilmedi.

Yerel, ignored kanıtlar: `.codex/integration/custom-audio-smoke/` altında
`cached-base-asr-872faf05367e44e5bde38143c8c259da/report.json`, `raw-asr.srt`,
`raw-asr-words.json`, `raw-asr-render/custom-audio.mp4`, `raw-asr-preview.png`.
Otomatik testlerin çıktıları ve RED/GREEN logları aynı `.codex/integration/`
tanılama alanındadır; test videoları Git'e eklenmez.

Bağımsız kod incelemesi eski altyazının yeni Whisper çıktısı sanılmasını
yakaladı; düzeltme ve regresyon testi sonrası son incelemede açık P1/P2 kalmadı.
Kurtarma ve UI değişiklikleri de ayrı incelendi. Upload modunda görünen mevcut
TTS ayar panelinin sadeleştirilmesi düşük öncelikli ayrı iştir; özel sesin
kullanımını engellemiyor ve bu pakette yeniden tasarlanmadı.

### Tam regresyon ve teslim kontrolü

| Ölçüm | Önceki paket | Bu paket |
|---|---:|---:|
| Yeni özel ses testleri | — | 20 doğrulama + 16 kurtarma + 4 gerçek render + 28 UI = **68** |
| Tam pytest | 1.440 geçti / 13 atlandı | **1.508 geçti / 13 atlandı** |
| Subtest | 12.162 | 12.174 |
| Uyarı | 3 | Aynı 3 |
| Branch dahil toplam coverage | %70,8894 | **%71,6187** |
| Kapsanan yürütülebilir satır | 17.366 / 23.385 | 17.569 / 23.453 |
| Kapsanan branch | 5.040 / 8.222 | 5.142 / 8.258 |
| `task.py` branch dahil coverage | %83,89 | %87,09 |
| Mevcut %70 kapısı | Geçti | Geçti, exit 0 |

Tam paket 136,78 saniye sürdü. `MPT_RUN_INTEGRATION_TESTS=0`,
`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` ve
`COVERAGE_FILE=.codex/integration/coverage-round3.coverage` ile önceki
coverage/pytest komutları çalıştırıldı. Yeni test atlama, coverage dışlaması
veya eşik düşürme yok. Ruff, compileall, dört yeni test dosyasının formatı
ve diff whitespace kontrolü başarılı. Kanıtlar: `coverage-round3-tests.log`,
`coverage-round3-report.txt`, `coverage-round3.json`.

Ana proje klasöründeki başlangıç yedeğine alınmış 30 dosyanın hash'i aynı kaldı.
Teslim patch'i yeni testler ve dil sözlüklerini içeriyor. Değişiklikler
`codex/integrate-upstream-v1.3.5` dalında yerel ve henüz commit edilmemiştir.
Bu tur Copilot'a yeni istek gönderilmedi; önceki 1,36 kredilik kullanım ayrı
kayıtta duruyor. Model indirme, ücretli API, hesap/anahtar değişikliği,
push, merge, yayın veya bulut kaynağı oluşturulmadı.

Sonraki yerel dilim **Docker/uv kurulum tutarlılığı** ve kendi GHCR hedefinin
güvenli hazırlığıdır. GitHub CI/CodeQL sonucu yeni kod push edilmeden mevcut
sayılmaz; push/yayın ayrıca yetkilendirilmiş somut teslim adımı olmalıdır.
Genel %80 coverage hedefi, Linux/Python 3.13 ve gerçek Redis ortamı açık işler
olarak kalır. Kullanıcının ElevenLabs API anahtarı kurmasına gerek yoktur.

## Dördüncü paket: Docker/uv ve güvenli imaj hedefi — 1 Eylül

Docker Desktop bu makinede kurulu fakat motor kapalıydı. Kullanıcının devam
isteğiyle motor arka planda açıldı; herhangi bir mevcut kullanıcı konteyneri
değiştirilmedi. Çalışma yine yalnız ayrı entegrasyon worktree'sinde yapıldı.

### Tek kilitli Python kurulumu

CPU ve GPU Dockerfile'larındaki `requirements.txt` + bağımsız pip çözümü
kaldırıldı. İki imaj da `pyproject.toml` ve `uv.lock` üzerinden
`uv sync --locked --no-dev --no-install-project` kullanıyor. Sanal ortam
`/opt/venv` altında; böylece host kaynak bağının ortamı gizlemesi önleniyor.
FFmpeg için hem uygulama hem MoviePy `/usr/bin/ffmpeg` kullanıyor. Kurulum
hatası zinciri durduruyor; eski `chmod 777`, PPA/system-pip ve başarısızlığı
maskeleyebilen shell fallback'leri yok.

Kurucu `uv 0.12.7`, CPU tabanı Python 3.11.16 slim-bookworm ve GPU tabanı
CUDA 12.3.2/cuDNN 9 olarak tam manifest digest'leriyle sabitlendi. GPU imajı
Python 3.11.16'nın 25 Ağustos 2026 tarihli immutable
python-build-standalone dağıtımını uv ile kullanacak. Bunun nedeni lock'taki
`ctranslate2 4.7.1` için cuDNN 9 gerekmesi; önceki cuDNN 8 tabanı uyumsuzdu.
[uv Docker kılavuzu](https://docs.astral.sh/uv/guides/integration/docker/),
[uv Python sürümleri](https://docs.astral.sh/uv/concepts/python-versions/),
[faster-whisper GPU koşulları](https://github.com/SYSTRAN/faster-whisper#gpu).

Gerçek son CPU imajı `linux/amd64`, Python 3.11.16 ve uv 0.12.7 ile başarıyla
oluştu. Yerel etiketi `mpt-local:uv-final-20260901`, image ID'si
`sha256:715fa52dbe5c3d4244b6f202414faf98bfdbb46248a05e01097819dbe2801d0e`,
mantıksal boyutu 767.783.860 bayt. Bu yerel etiket yayın veya immutable uzak
digest değildir.

İmaj içinde host config/storage/model/agent/git tanılama dosyası bağlanmadan,
ağ kapalı şekilde beş kabul testi geçti: doğrudan ve transitive kurulu sürümler
lock ile eşleşti; Pillow override'ı uygulandı; geliştirme paketleri `/opt/venv`
içinde yok; kritik native/application import'ları açıldı; Türkçe altyazılı,
sesli 1080×1080 video gerçek FFmpeg ile üretildi ve güvensiz yerel materyal
reddi çalıştı. Gerçek Streamlit `AppTest` 0 exception ile 27 düğme, 29 seçim
kutusu ve 2 dosya yükleyici oluşturdu. Ayrı ağsız süreç kontrolünde:

| İç URL | Sonuç |
|---|---:|
| `http://127.0.0.1:8080/ping` | 200 |
| `http://127.0.0.1:8080/docs` | 200 |
| `http://127.0.0.1:8501/_stcore/health` | 200 |

İlk denemede API süreci çalışmasına rağmen `/ping` 404 verdi. Hazır ping
controller'ının root router'a hiç bağlanmadığı önce başarısız testle gösterildi;
küçük router bağlantısı sonrası aynı test ve gerçek konteyner isteği 200 verdi.

### Compose ve yayın sınırı

Normal Compose artık host çalışma ağacının tamamını imajın üstüne bağlamıyor;
yalnız mevcut `config.toml` ve `storage` bağlanıyor. Eksik config'in sessizce
klasöre dönüşmemesi için `create_host_path: false` kullanılıyor. Yerel, GPU ve
release birleşimleri boş bir test klasöründe gerçek `docker compose config`
ile çözüldü. Bütün servisler yalnız yayımlanan/hedeflenen `linux/amd64`
platformunu açıkça belirtiyor; ARM hostta amd64 emülasyonu gerekir.

Release Compose artık upstream `ghcr.io/harry0703/...:latest` kullanmıyor.
`MPT_IMAGE` verilmezse erken hata veriyor; yalnız kullanıcının kendi tam SHA
etiketi veya tercihen `image@sha256:digest` referansı kabul ediliyor. README'nin
İngilizce/Çince Docker bölümleri hem yerel build hem açık imaj referansı için
güncellendi. [Compose zorunlu değişken sözdizimi](https://docs.docker.com/reference/compose-file/interpolation/),
[GHCR digest ile çekme](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

GHCR workflow yalnız elle başlatılıyor ve varsayılanı `publish=false`.
Build işi `contents: read` ile kendi repo adından küçük harfli imaj adı ve tam
commit SHA etiketi üretip imajı ağsız test ediyor. Yayın işi yalnız açık seçim +
repo varsayılan dalında `packages: write` alıyor; göndereceği yerel imajı tekrar
test ediyor, sonra aynı etiketi push ediyor ve yalnız beklenen repository için
tek geçerli SHA-256 digest'i çıktı kabul ediyor. Action'lar tam commit SHA ile
sabit. `actionlint 1.7.12`, Compose kontrolleri ve digest seçim shell'inin altı
çevrimdışı sahte-Docker vakası geçti. [GitHub'ın resmi imaj yayınlama modeli](https://docs.github.com/actions/guides/publishing-docker-images).

Workflow **çalıştırılmadı**; GHCR login/push, commit, merge, release veya uzak
digest oluşmadı. CPU imajı yalnız yerelde kaldı. NVIDIA GPU bulunmadığından
GPU taban digest/uyumluluk araştırması, BuildKit `--check` ve Compose rezervasyon
kontrolü geçti; ağır GPU imajı indirilmedi ve CUDA/Whisper/NVENC runtime sonucu
iddia edilmiyor. Bu açık bir sonraki GPU donanımlı ortam kabul kontrolüdür.

Bağımsız son inceleme önce ARM/platform ve imajda dev dependency regresyonu
eksiklerini P2 olarak buldu. Platform pinleri ve beşinci imaj testi eklendikten
sonra ilgili kontroller geçti; açık P1 kalmadı. Yerel uygulama dokümanındaki
`venv + pip` yolu legacy uyumluluk içindir ve uv lock kadar tekrarlanabilir
değildir; güvenilir kurulum için uv yolu önerilir.

### Dördüncü paket teslim kontrolü

| Ölçüm | Sonuç |
|---|---:|
| Son tam pytest | **1.513 geçti / 13 atlandı** |
| Subtest / uyarı | 12.326 geçti / önceki 3 uyarı aynı |
| Branch dahil coverage | **%71,6281**; %70 kapısı exit 0 |
| Kapsanan yürütülebilir satır | 17.576 / 23.460 |
| Kapsanan branch | 5.143 / 8.258 |
| `app/router.py` coverage | %100 |
| Container unittest | 5 geçti; ağ kapalı, dev dependency yok |
| Odaklı API/container paketi | 27 geçti / 3 platform skip / 167 subtest |
| Gerçek Streamlit | 0 exception; 27 button / 29 selectbox / 2 uploader |
| Gerçek container HTTP | API ping/docs ve WebUI health: üçü de 200 |

Son düzeltmelerden sonraki tam paket 94,20 saniye sürdü. Coverage ölçümü aynı
üretim kodunda bir önceki tam koşudan alındı; son ek test yalnız container
ortamında dev paketlerin bulunmamasını doğruluyor ve ayrıca hedefli geçti.
Ruff, format kontrolü, compileall, diff whitespace, gerçek Docker build,
BuildKit GPU Dockerfile kontrolü ve üç Compose sözleşmesi başarılı.

Kanıtlar `.codex/integration/container-round4/`,
`coverage-round4-tests.log`, `coverage-round4-report.txt` ve
`coverage-round4.json` altında ignored yerel dosyalardır. Son değişiklik özeti,
başlangıçta yedeklenen 30 özgün dosyanın hâlâ aynı olduğunu doğruladı. Son CPU
imajı yerelde tutuldu; daha eski bu tura ait test etiketi kaldırıldı. Bu turda
Copilot isteği veya AI kredi tüketimi olmadı.

## GitHub PR kabul turu — 1 Eylül

Yerel entegrasyon iki commit halinde `codex/integrate-upstream-v1.3.5` dalına
alındı ve `medicanasa-hue/money-agent-work` deposunda taslak PR #4 açıldı. İlk
uzak koşuda CodeQL Python `security-extended` analizi başarıyla tamamlandı.
Linux Python 3.11 ve 3.13 CI işleri de tam test ve coverage adımlarıyla geçti.

Windows işi 1.510 test geçtikten sonra altı path assertion'ında durdu. GitHub
runner aynı gerçek temp dizinini test tarafında `C:\Users\RUNNER~1`, uygulama
tarafında `C:\Users\runneradmin` biçiminde gösteriyordu. Production davranışı
değiştirilmedi; dosyalar temp context içinde hâlâ mevcutken
`Path.resolve(strict=True)` ile canonical eşdeğerlik sınandı. Mock çağrı sayısı,
kwargs ve diğer davranış assertion'ları korundu. Üç ilgili dosyadaki 49 test hem
normal temp diziniyle hem gerçek bir Windows 8.3 alias üzerinden geçti.

İlk koşu ayrıca Node.js 20 action deprecation uyarısı verdi. Resmî action
release ve `action.yml` kayıtları doğrulanarak checkout 7.0.1, setup-python
7.0.0, setup-uv 10.0.1 ve upload-artifact 7.0.1 Node 24 sürümlerine geçirildi.
Dependency audit action'ı da yayımlanmış 1.1.0 commit'ine sabitlendi. Dört
workflow'daki 19 `uses:` girdisinin tamamı artık değişmez 40 haneli commit SHA
kullanıyor; altı checkout adımının tamamında credentials kalıcı değil.
[checkout 7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1),
[setup-python 7.0.0](https://github.com/actions/setup-python/releases/tag/v7.0.0),
[setup-uv 10.0.1](https://github.com/astral-sh/setup-uv/releases/tag/v10.0.1),
[upload-artifact 7.0.1](https://github.com/actions/upload-artifact/releases/tag/v7.0.1).

Push sırasında GitHub varsayılan dal için 70 açık Dependabot kaydı bildirdi.
Bunlar 62 benzersiz pip paket/advisory eşleşmesi: PR lock sürümleri 61'ini
güvenli aralığa taşıyor, kalan `httplib2` artık lock'ta yok. GitPython'ın
`= 3.1.50` biçimli kaydı ayrıca elle kontrol edildi; PR sürümü 3.1.58. Uzak
uyarılar yalnız varsayılan dal güncellendiğinde yeniden hesaplanacağından PR
taslak haldeyken açık görünmeleri bekleniyor.

### CodeQL güvenlik düzeltme turu

İkinci PR koşusunda normal CI ve workflow içindeki CodeQL işi yeşile döndü;
GitHub Advanced Security'nin ayrı `CodeQL` kapısı ise üç yeni uyarıyla PR'ı
`UNSTABLE` tuttu: Google News RSS konu parametresinden kısmi SSRF, RSS XML
payload'unda entity/DTD riski ve materyal cache anahtarında zayıf MD5 kullanımı.

RSS istemcisi sabit `https://news.google.com/rss/search` endpoint'ine alındı;
kullanıcı konusu yalnız `params["q"]` olarak gönderiliyor, Türkçe locale
parametreleri ayrıca ekleniyor. RSS istekleri redirect takip etmiyor, response
stream'i 1 MiB ile sınırlandırılıyor ve parse `defusedxml.ElementTree` ile
DTD/entity/external referansları kapalı çalışıyor. `defusedxml==0.7.1`
`pyproject.toml`, `requirements.txt` ve `uv.lock` içine kilitlendi.

Materyal video, image ve preview indirme yüzeyi aynı güvenlik turunda
sertleştirildi. `app/services/url_security.py` HTTP(S) dışı URL'leri, userinfo
içeren URL'leri, whitespace/control karakterlerini, literal private IP'leri,
IPv4-mapped IPv6 private adresleri, boş DNS cevaplarını ve public/private karışık
DNS sonuçlarını fail-closed reddediyor. Materyal indirme helper'ı her redirect
hop'unu en fazla üç adımla ve `allow_redirects=False` ile elle takip ediyor;
redirect hedefleri yeniden aynı public DNS kapısından geçiyor, provider'a özel
image redirect validator'ı ek kapı olarak korunuyor ve response nesneleri
kapatılıyor. Public DNS sonrası private connected peer görülürse proxy yokken
istek reddediliyor.

Cache dosya isimleri için `utils.md5` kaldırıldı; video/image cache anahtarları
deterministik 128-bit SHA-256 prefix'i üreten `utils.stable_cache_key` ile
hesaplanıyor. Dosya adı uzunluğu eski 32 hex karakterlik yapıda kaldığından
filesystem yüzeyi büyümedi; eski cache dosyaları yeniden indirilebilir, bu kabul
edilen davranış değişikliğidir.

Bu tur için önce başarısız güvenlik regresyon testleri yazıldı, sonra üretim
düzeltmeleri uygulandı. Son doğrulama: `uv lock --check`, `uv sync --frozen`,
`ruff check app test`, `compileall app test`, tam `pytest` ve `uvx pip-audit
--local` geçti. Tam suite sonucu **1.540 geçti / 13 atlandı / 3 mevcut uyarı**
ve `pip-audit` sonucu "No known vulnerabilities found". Uzak CodeQL yeniden
koşusu bu commit pushlandıktan sonra izlenecek.
