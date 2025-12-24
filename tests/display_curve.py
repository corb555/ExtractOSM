import matplotlib.pyplot as plt
import numpy as np

def calculate_distance_score(distance, excellent_dist=20, good_dist=30, poor_dist=50, unacceptable_dist=110):
    """
    Translates a raw distance in meters into a quality score from 0 to 100
    based on a piecewise linear function.
    """
    if distance <= excellent_dist:
        # --- Excellent Zone (Score: 100 down to 95) ---
        progress_in_zone = distance / excellent_dist
        return 100 - (progress_in_zone * 5)

    elif distance <= good_dist:
        # --- Very Good Zone (Score: 95 down to 90) ---
        progress_in_zone = (distance - excellent_dist) / (good_dist - excellent_dist)
        return 95 - (progress_in_zone * 5)

    elif distance <= poor_dist:
        # --- Acceptable Zone (Score: 90 down to 70) ---
        progress_in_zone = (distance - good_dist) / (poor_dist - good_dist)
        return 90 - (progress_in_zone * 20)

    elif distance <= unacceptable_dist:
        # --- Poor Zone (Score: 70 down to 45) ---
        progress_in_zone = (distance - poor_dist) / (unacceptable_dist - poor_dist)
        return 70 - (progress_in_zone * 70)

    else:
        # --- Unacceptable Zone ---
        return 0

# --- Plotting Code ---

# Generate a range of distances from 0 to 120 meters
distances = np.linspace(0, 120, 500)
# Calculate the score for each distance
scores = [calculate_distance_score(d) for d in distances]

# Create the plot
plt.figure(figsize=(10, 6))
plt.plot(distances, scores, lw=3, color='royalblue')

# --- Add annotations and styling to make it clear ---
plt.title('Distance to Quality Score Conversion Curve', fontsize=16)
plt.xlabel('Distance from Auxiliary Point (meters)', fontsize=12)
plt.ylabel('Distance Quality Score (0-100)', fontsize=12)
plt.grid(True, which='both', linestyle='--', linewidth=0.5)

# Define the zones for annotation
zones = {
    'Excellent': (0, 20, 97.5),
    'Very Good': (20, 30, 92.5),
    'Acceptable': (30, 50, 80),
    'Poor': (50, 100, 30),
    'Unacceptable': (100, 120, 10)
}

# Add vertical lines and text for each zone
for name, (start, end, y_pos) in zones.items():
    plt.axvline(x=start, color='gray', linestyle=':', alpha=0.7)
    if name != 'Unacceptable':
        plt.axvline(x=end, color='gray', linestyle=':', alpha=0.7)

    # Place text in the middle of the zone
    mid_point = start + (end - start) / 2
    plt.text(mid_point, y_pos, name, ha='center', va='center', fontsize=11,
             bbox=dict(facecolor='white', alpha=0.8, edgecolor='none', pad=0.2))

# Set axis limits
plt.xlim(0, 120)
plt.ylim(0, 105)

# Add key points to the plot
key_points = [0, 20, 30, 50, 100]
key_scores = [calculate_distance_score(d) for d in key_points]
plt.scatter(key_points, key_scores, color='red', zorder=5, s=50)

# Label the key points
for d, s in zip(key_points, key_scores):
    plt.text(d + 1, s - 4, f'({d}m, {s})', fontsize=9, color='darkred')

plt.tight_layout()
plt.show()