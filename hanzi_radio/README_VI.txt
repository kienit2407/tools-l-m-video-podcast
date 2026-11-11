HANZI RADIO — TIẾNG TRUNG HSK 5–6

Luồng riêng cho tiếng Phổ thông, chữ giản thể và phụ đề tiếng Việt.
Band 7 Radio tiếng Anh vẫn dùng one_click.py và các thư mục cũ.

1. CHUẨN BỊ

Kịch bản Trung: hanzi_radio/kich_ban_tieng_trung/kich_ban.txt
Bản Việt:      hanzi_radio/kich_ban_tieng_viet/kich_ban.txt

Dùng MIA: và ALEX: như tool tiếng Anh. Có thể thêm một trong 12 tag cảm xúc
cơ bản ngay sau nhãn người nói ở bản Trung; xem README_VI.txt ngoài thư mục
gốc. Tag không hiện trên phụ đề và không cần đặt ở bản Việt.
Hai bản phải khớp số lượt và thứ tự. Cũng chấp nhận dấu hai chấm ：.
HSK 5–6 là mục tiêu của nội dung: tool đọc nguyên văn, không tự nâng/hạ
trình độ hay viết lại lời thoại. Dùng PROMPT_KICH_BAN_HSK5_6.txt để soạn.
File mặc định là hội thoại mẫu ngắn về lựa chọn nghề nghiệp, có bản dịch.

2. KIỂM TRA MIỄN PHÍ, KHÔNG GỌI API

python3 one_click_hanzi.py --episode HZ01_GEMINI --dry-run

Không tạo giọng, không tải model, không xuất video ở chế độ này.
Dùng chung thư viện trong requirements.txt, không có model TTS local mới.
Whisper dùng model base đã có nếu máy đã tải; chạy thật có thể tải nếu
bạn chọn một model Whisper khác chưa có trong cache.

3. CHẠY GEMINI

python3 one_click_hanzi.py --episode HZ01_GEMINI

API key: dùng GEMINI_API_KEY trong generate.py hoặc biến môi trường cùng tên.
Cũng có thể điền GEMINI_API_KEY riêng ở đầu one_click_hanzi.py để dùng
project khác cho Hanzi. Biến môi trường được ưu tiên nếu đã thiết lập.
Nếu dùng cùng project Google, hai kênh dùng chung quota của project đó.
Mặc định Mia = Kore, Alex = Puck; cùng giọng nhưng hướng dẫn nói Phổ thông,
không còn hướng dẫn accent Anh/Mỹ. Không tự thay đổi tốc độ âm thanh.
Không gửi mode trong speech_config, chỉ dùng speakers như luồng đã chạy.

Lần chạy thật đầu tiên: tạo một intro tiếng Trung ngắn, mất thêm một yêu
cầu TTS. Intro được lưu ở hanzi_radio/assets/intro/<mã-cấu-hình>/intro.wav,
dùng lại ở các tập sau cùng provider/model/giọng. Đổi giọng/model sẽ tạo
intro tương ứng khác. Không dùng intro tiếng Anh của Band 7 Radio.
Hội thoại dài chia theo lượt, tối đa khoảng 800 ký tự mỗi yêu cầu Gemini.

4. ELEVENLABS (TÙY CHỌN)

python3 one_click_hanzi.py --tts-provider elevenlabs --episode HZ01_ELEVENLABS

Key dùng chung với generate_elevenlabs.py nếu không điền key riêng.
Nếu muốn key riêng, điền ELEVENLABS_API_KEY ở đầu one_click_hanzi.py.
Điền Voice ID tiếng Phổ thông cho ELEVENLABS_MIA_VOICE_ID và
ELEVENLABS_ALEX_VOICE_ID ở one_click_hanzi.py,
hoặc truyền --mia-voice và --alex-voice. Giọng của Hanzi không tự lấy hai
Voice ID tiếng Anh đã điền cho Band 7 Radio.
Giới hạn giọng cộng đồng qua API của gói Free ElevenLabs vẫn áp dụng.

5. VIDEO VÀ CHẠY TIẾP

Kết quả: hanzi_radio/episodes/HZ01_GEMINI/final/VIDEO_FINAL.mp4
Sau khi xuất xong, tool in ba mốc nghe để sao chép vào mô tả YouTube và
lưu thêm final/YOUTUBE_CHAPTERS.txt (transcript Trung, phụ đề Trung–Việt).
1: chữ Hán cuộn theo hội thoại.
2: chữ Hán và phụ đề Việt ở giữa khung hình, không có logo và waveform.
3: chỉ nghe, không phụ đề.
Logo chữ: Hanzi Radio. Thẻ giới thiệu và hướng dẫn nghe bằng tiếng Trung.
Không hiển thị tiêu đề tập trên các thẻ, giống cấu hình hiện tại.

Chạy lại đúng lệnh để tiếp tục, phần đã hoàn tất được dùng lại. Không xóa
cache khi hết quota. Đổi script/voice/model/provider thì dùng episode mới.
Cache và đầu ra Hanzi tách riêng các episode tiếng Anh.
File phụ đề: chinese.srt, vietnamese.srt, bilingual.srt trong work/.
ASR được đặt language=zh và canh theo chữ Hán; mốc từng chữ trong một từ
ASR nhiều chữ được nội suy trong khoảng thời gian của từ đó. Cần nghe và
kiểm tra timing trước khi đăng, nhất là tên riêng, con số và từ nước ngoài.

6. AUDIO CÓ SẴN VÀ FONT

python3 one_click_hanzi.py --episode HZ02_IMPORT --reuse-audio "/duong/dan/hoi_thoai.wav"

WAV phải là đúng kịch bản Trung và chưa thêm khoảng lặng đầu. Chế độ này
không tạo giọng mới. Nếu chưa có intro tương ứng, thẻ intro hiển thị im
lặng; có thể cung cấp intro Trung có sẵn bằng --intro-audio "/duong/dan/intro.wav".
--intro-audio cũng dùng được khi tạo hội thoại qua API.

Mac dùng face Heiti SC giản thể của font hệ thống, được chuẩn bị trong
thư mục tạm của máy; không tải font hay đóng gói font Apple vào dự án.
Có thể chọn font khác bằng
--font-path "/duong/dan/NotoSansSC.ttf". Font Việt theo VI_FONT_PATH trong
one_click.py, hoặc --vi-font-path. Font chữ Hán được kiểm tra ký tự trước TTS.
Không cần cài font Trung vào hệ thống nếu chỉ định file font trực tiếp.
Các cỡ chữ và bố cục dùng chung one_click.py để dễ đồng bộ hai kênh.

Khi cần tăng độ chính xác nhận dạng, có thể chọn --whisper-model small;
model lớn hơn có thể cần tải thêm. Không bắt buộc để bắt đầu.

Các kiểm tra phát triển không gọi TTS thật. Chất lượng giọng Phổ thông và
timing âm thanh thật chỉ xác nhận được sau khi bạn chạy một tập thực tế.
