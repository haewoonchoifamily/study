from moviepy.editor import VideoFileClip

# 원본 영상 로드
video_path = "거양금속 샘플.mp4"

# 1. CAM1 구간 (0초 ~ 4.5초) GIF 변환
clip1 = VideoFileClip(video_path).subclip(0, 4.5)
clip1.write_gif("cam1_result.gif", fps=10)

# 2. CAM2 구간 (4.5초 ~ 끝까지) GIF 변환
clip2 = VideoFileClip(video_path).subclip(4.5, 9.0)
clip2.write_gif("cam2_result.gif", fps=10)