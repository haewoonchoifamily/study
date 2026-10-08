# -*- coding: utf-8 -*-
"""COCO 클래스 이름 영문 -> 한글.

torchvision(91개, 빈칸 포함)과 ultralytics YOLO(80개) 모두 영문 이름을 쓰므로
영문 이름을 키로 둔다. 표에 없는 이름은 영문 그대로 쓴다.
"""

COCO_KO = {
    "person": "사람",
    "bicycle": "자전거",
    "car": "자동차",
    "motorcycle": "오토바이",
    "airplane": "비행기",
    "bus": "버스",
    "train": "기차",
    "truck": "트럭",
    "boat": "배",
    "traffic light": "신호등",
    "fire hydrant": "소화전",
    "stop sign": "정지 표지판",
    "parking meter": "주차 미터기",
    "bench": "벤치",
    "bird": "새",
    "cat": "고양이",
    "dog": "개",
    "horse": "말",
    "sheep": "양",
    "cow": "소",
    "elephant": "코끼리",
    "bear": "곰",
    "zebra": "얼룩말",
    "giraffe": "기린",
    "backpack": "배낭",
    "umbrella": "우산",
    "handbag": "핸드백",
    "tie": "넥타이",
    "suitcase": "여행가방",
    "frisbee": "프리스비",
    "skis": "스키",
    "snowboard": "스노보드",
    "sports ball": "공",
    "kite": "연",
    "baseball bat": "야구 배트",
    "baseball glove": "야구 글러브",
    "skateboard": "스케이트보드",
    "surfboard": "서핑보드",
    "tennis racket": "테니스 라켓",
    "bottle": "병",
    "wine glass": "와인잔",
    "cup": "컵",
    "fork": "포크",
    "knife": "칼",
    "spoon": "숟가락",
    "bowl": "그릇",
    "banana": "바나나",
    "apple": "사과",
    "sandwich": "샌드위치",
    "orange": "오렌지",
    "broccoli": "브로콜리",
    "carrot": "당근",
    "hot dog": "핫도그",
    "pizza": "피자",
    "donut": "도넛",
    "cake": "케이크",
    "chair": "의자",
    "couch": "소파",
    "potted plant": "화분",
    "bed": "침대",
    "dining table": "식탁",
    "toilet": "변기",
    "tv": "TV",
    "laptop": "노트북",
    "mouse": "마우스",
    "remote": "리모컨",
    "keyboard": "키보드",
    "cell phone": "휴대폰",
    "microwave": "전자레인지",
    "oven": "오븐",
    "toaster": "토스터",
    "sink": "싱크대",
    "refrigerator": "냉장고",
    "book": "책",
    "clock": "시계",
    "vase": "꽃병",
    "scissors": "가위",
    "teddy bear": "곰인형",
    "hair drier": "헤어드라이어",
    "toothbrush": "칫솔",
}


# open-vocabulary(YOLO-World)용으로 실행 중에 더해지는 표시 이름.
# 사용자가 프롬프트에 "door=문" 처럼 적으면 여기 들어온다.
EXTRA_KO: dict[str, str] = {}


def add_alias(en: str, ko: str) -> None:
    """영문 이름 하나에 화면 표시용 한글 이름을 달아 둔다."""
    if en and ko:
        EXTRA_KO[en] = ko


def to_ko(name: str) -> str:
    """영문 클래스명을 한글로. 표에 없으면 그대로 돌려준다."""
    return EXTRA_KO.get(name) or COCO_KO.get(name, name)
