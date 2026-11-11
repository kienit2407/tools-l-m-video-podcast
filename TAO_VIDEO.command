#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 one_click.py --interactive "$@"
result=$?
if [[ $result -ne 0 ]]; then
    print "Tạo video chưa hoàn tất. Các file đã tạo được giữ lại để chạy tiếp."
fi
read -r "?Nhấn Enter để đóng cửa sổ..."
exit $result
