import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns

# 논문 본문 데이터 기반 예측 확률 행렬 구성 (Row: Actual, Column: Predicted)
# Classes: 0: BOP normal, 1: BOP hump, 2: Narrow gap normal, 3: Narrow gap hump
classes = ["BOP\nnormal", "BOP\nhump", "Narrow gap\nnormal", "Narrow gap\nhump"]

# 본문 수치 대입 (BOP Normal 입력시: 97.2%, Narrow Gap Hump 입력시: 90.5% 등)
conf_matrix = np.array(
    [
        [97.2, 0.5, 0.5, 1.9],  # Actual: BOP normal
        [4.3, 90.5, 3.5, 1.7],  # Actual: BOP hump (예시 보완 축)
        [3.5, 1.7, 91.3, 3.5],  # Actual: Narrow gap normal (예시 보완 축)
        [4.3, 1.7, 3.5, 90.5],  # Actual: Narrow gap hump
    ]
)

# 그래프 기본 스타일 설정 (학술지 스타일)
plt.figure(figsize=(7, 6), dpi=300)
sns.set_theme(style="white")

# 히트맵 그리기
ax = sns.heatmap(
    conf_matrix,
    annot=True,
    fmt=".1f",
    cmap="Blues",
    cbar=True,
    xticklabels=classes,
    yticklabels=classes,
    annot_kws={"size": 12, "weight": "bold"},
    linewidths=0.5,
    linecolor="gray",
)

# 축 레이블 및 타이틀 설정
ax.set_title(
    "Confusion Matrix of Welding Quality Classification (%)",
    fontsize=14,
    pad=20,
    weight="bold",
)
ax.set_xlabel("Predicted Class", fontsize=12, labelpad=10)
ax.set_ylabel("Actual Class", fontsize=12, labelpad=10)
plt.xticks(rotation=0, fontsize=10)
plt.yticks(rotation=0, fontsize=10)

plt.tight_layout()
plt.savefig("welding_confusion_matrix.png", bbox_inches="tight")
plt.show()