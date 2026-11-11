TẠO VIDEO BAND 7 RADIO

1. Đặt kịch bản Anh vào kich_ban_tieng_anh/kich_ban.txt và bản dịch Việt vào
   kich_ban_tieng_viet/kich_ban.txt. Hai bản phải có cùng số lượt và thứ tự Mia:/Alex:.
2. Bấm đúp TAO_VIDEO.command (hoặc chạy python3 one_click.py trong Terminal).
3. Chọn trình độ: 1 = A1–A2, 2 = B1–B2. Enter giữ B1–B2 như trước.
4. Tool xuất video ngang 16:9 luôn, không hỏi chọn khung hình.

A1–A2 dùng Gemini, yêu cầu đọc chậm/rõ hơn, khoảng 90–105 từ/phút và nghỉ
giữa câu lâu hơn. B1–B2 giữ cách đọc cũ, khoảng 130–145 từ/phút. Đây là chỉ dẫn
cho TTS, không phải tốc độ bảo đảm. Tool không tự viết lại hay hạ độ khó kịch bản.
ElevenLabs hiện giữ cách đọc mặc định và chỉ hỗ trợ lựa chọn B1–B2 trong menu này.

Video 1080p có kích thước 1920×1080, mặc định 60 fps và giữ đủ ba lần nghe.
Mở đầu chỉ hiện chữ trong 2 giây, không có lời chào hay giọng đọc giới thiệu,
rồi chuyển vào Part 1. Màn hình liệt kê ba phần bằng tiếng Việt:
Part 1 có transcript Anh, Part 2 có phụ đề Anh–Việt, Part 3 chỉ nghe không phụ đề.
Không gọi TTS cho đoạn mở đầu. Giọng đọc bài học và outro giữ nguyên.
Nếu cần lời chào riêng, có thể truyền WAV qua --intro-audio.
Part 1 cuộn như teleprompter: canh dòng đang được đọc quanh giữa màn hình bằng
thời gian từng từ từ Whisper. Tốc độ thay đổi từ từ theo nhịp nói; toàn bộ đường
cuộn được làm mượt trên cửa sổ một giây để tránh tăng tốc đột ngột giữa hai câu.
Cache cũ chưa có thời gian từng từ vẫn dùng được với ước lượng trong từng lượt.
Giữ 60 fps. Part 2 vẫn dùng phụ đề theo tiếng.

Kết quả: episodes/<tên tập>/final/VIDEO_FINAL.mp4 (ngang).
Chạy lại cùng tên tập và cùng trình độ để dùng
lại audio. Đổi trình độ cần tên tập mới; tên mặc định A1–A2 có thêm _A1_A2.
Chỉnh chuyển động chữ không cần tạo lại giọng. Khi thay kịch bản cho video mới, dùng
--episode với tên tập mới để giữ các tập cũ.

Chạy lệnh không cần menu:
python3 one_click.py --level a1-a2 --episode EP53_A1

Kiểm tra đầu vào, không gọi API hay dựng video:
python3 one_click.py --level a1-a2 --episode EP53_A1 --dry-run

GHI CHÚ THAM KHẢO CHO KÊNH TIẾP THEO (không phải chức năng của tool hiện tại)
Cấu trúc mẫu cho một tập IELTS Lab
1. 00:00 — Question Preview
   
   Giới thiệu Part, dạng câu hỏi và cho người học thời gian đọc trước 6–10 câu. Không hiện đáp án.
2. 00:30 — Test Mode
   
   Phát bài nghe một lần, không phụ đề, không bản dịch. Giữ tốc độ và điều kiện nghe tương đối giống đề.
3. 06:00 — Answer Check
   
   Hiển thị đáp án, chỉ ra chính xác câu nói chứa bằng chứng và phân tích các distractors.
4. 09:00 — Transcript & Vocabulary
   
   Phát lại các đoạn quan trọng với transcript EN, giải thích paraphrase, collocations và cách người nói diễn đạt.
5. 12:00 — Shadowing Practice
   
   Chọn một số câu khó, cho người học nghe và nhắc lại theo nhóm ngữ điệu. Phần này là luyện kỹ năng, không tính vào điểm mock test.
Các mốc thời gian là mẫu bố cục minh họa, không phải timestamps của video đã có.

tham khảo cho kênh tiếp theo


python3 one_click.py --tts-provider gemini --episode EP73_GEMINI
