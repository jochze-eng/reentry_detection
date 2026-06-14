# Image Integration & Bounding Box Rendering Guide

This guide details the implementation of the Face Grouping Dashboard and details panel, specifically focusing on how the face crop and scene images are retrieved via APIs, how they are proxied to avoid CORS and security issues, how they are displayed side-by-side, and how the face bounding boxes are dynamically drawn.

---

## 1. System Architecture & Routing (Image Proxying)

The application communicates with a remote **Vaidio AI NVR** instance (`VAIDIO_HOST`). Since the client browser should not directly request images from the Vaidio server (to prevent CORS issues and avoid exposing Vaidio credentials/addresses), the frontend uses an **Nginx reverse proxy**.

### Nginx Routing Setup (`nginx.conf.template`)
Any request from the browser starting with `/ainvr/` is intercepted by Nginx and proxied directly to the `VAIDIO_HOST`.

```nginx
server {
    listen 80;
    server_name localhost;

    # Serve Vue SPA static files
    location / {
        root /usr/share/nginx/html;
        index index.html;
        try_files $uri $uri/ /index.html;
    }

    # Proxy local API requests to Python backend
    location /api {
        proxy_pass http://backend:9000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }

    # Proxy Vaidio image and media requests
    location /ainvr {
        proxy_pass ${VAIDIO_HOST};
        proxy_set_header Host $proxy_host;
        proxy_ssl_server_name on;
    }
}
```

---

## 2. API Endpoints & Image Paths

The python backend queries Vaidio via the Vaidio Core APIs, processes the paths, and returns them to the frontend.

### A. Backend Image Stripping
When the backend fetches face information (`GET /ainvr/api/face/{faceKeyId}`) from Vaidio, the Vaidio API returns a full URL in the `file` field:
- **Vaidio Response `file`**: `http://100.101.159.22/ainvr/storage/vcore/2026-06-12/123_face_0_crop.jpg`

The backend strips the protocol and host using regex to keep the path relative:
```python
# Strip host/protocol (leaves "ainvr/storage/vcore/2026-06-12/123_face_0_crop.jpg")
file_relative_path = re.sub(r'http[s]?://[^/]+/', '', face_info.file)
```
This relative path is stored in the database (`face_events` table, `file` column).

### B. Addon API Image Resolution
When the frontend requests list/detail data, the backend constructs the full image URLs pointing to its own host (so Nginx can proxy them):
```python
# Construct URL pointing back to local host
file_url = f"{fast_req.url.scheme}://{vaidio_public_host(fast_req.url.hostname)}:{self.resolver.port(fast_req.url.scheme)}/{face.file}"
# Result: http://localhost/ainvr/storage/vcore/2026-06-12/123_face_0_crop.jpg
```

---

## 3. Frontend Image Derivation (Crop vs. Scene)

Vaidio stores the cropped face and the original scene snapshot in the same folder with a suffix naming convention:
- **Face Crop File**: `.../123_face_0_crop.jpg` (returned by API in `item.file`)
- **Original Scene File**: `.../123_face_0.jpg`

The frontend derives the scene image URL (`scenesrc`) dynamically by stripping the `_crop` suffix:

```javascript
// Remove "_crop" suffix and join back with the file extension
item.scenesrc = `${item.file.slice(0, item.file.lastIndexOf('_'))}.${item.file.split('.').pop()}`;
// Result: http://localhost/ainvr/storage/vcore/2026-06-12/123_face_0.jpg
```

---

## 4. UI Components & Layouts

### A. Dashboard Sidebar List (Recent Face Groups)
- **Component**: [FrDashboard.vue](file:///Volumes/ExternalSSD/IronyunDrive/Ironyun_Drive/AINVR%20Sample%20code/ps_addon_grouping/frontend/src/components/facegroup/FrDashboard.vue) (Left Panel)
- **Child Component**: [TargetCardHorizontal.vue](file:///Volumes/ExternalSSD/IronyunDrive/Ironyun_Drive/AINVR%20Sample%20code/ps_addon_grouping/frontend/src/components/facegroup/TargetCardHorizontal.vue)
- **API Used**: `POST /api/facegroup/faces/latest`
- **Render Details**: Displays circular face crop (`target.file`), name, category list label, demographic info, and appearance count (`repeats`).

### B. Group Details View (Detections Grid)
- **Component**: [FrDashboard.vue](file:///Volumes/ExternalSSD/IronyunDrive/Ironyun_Drive/AINVR%20Sample%20code/ps_addon_grouping/frontend/src/components/facegroup/FrDashboard.vue) (Right Panel)
- **Child Components**: [TimeBarChart.vue](file:///Volumes/ExternalSSD/IronyunDrive/Ironyun_Drive/AINVR%20Sample%20code/ps_addon_grouping/frontend/src/components/facegroup/TimeBarChart.vue), [SceneGridView.vue](file:///Volumes/ExternalSSD/IronyunDrive/Ironyun_Drive/AINVR%20Sample%20code/ps_addon_grouping/frontend/src/components/facegroup/SceneGridView.vue)
- **API Used**: `GET /api/facegroup/faces/{faceKeyId}/matches?time_offset=xxx`
- **Render Details**:
  - The ECharts bar chart shows count trends.
  - `<scene-grid-view>` shows a grid of cards containing the scene image (`item.scenesrc`) on the left and the face crop (`item.file`) on the right side-by-side, along with a confidence tag overlay (e.g. `83%`).
  - To display side-by-side, the CSS uses the `.face-searched` helper class:
  ```scss
  .result-detail-item {
      overflow: hidden;
      height: 126px;
      display: flex;
      
      .result-detail-thumbnail {
          border-radius: 2px 2px 0 0;
          overflow: hidden;
          height: 100%;
      }
      .result-mathch-face {
          display: block; // Displays face crop next to scene thumbnail
          position: relative;
          box-shadow: -3px 0 #fff;
          height: 100%;
          width: 126px; // Fixes face crop as a square
      }
  }
  ```

---

## 5. Dynamic Bounding Box & Zoom Modal

Clicking any item in the detections grid opens [DetailDialog.vue](file:///Volumes/ExternalSSD/IronyunDrive/Ironyun_Drive/AINVR%20Sample%20code/ps_addon_grouping/frontend/src/components/facegroup/DetailDialog.vue).

### A. Drawing Bounding Boxes
The API returns a `position` field which is an array `[left, top, width, height]` in pixels relative to the original (natural) resolution of the scene snapshot.

Since the image is scaled by the browser to fit the container:
1. Load the image in memory first to determine its natural dimensions:
   ```javascript
   function imageIsLoaded(image) {
       return new Promise(resolve => {
           image.onload = () => resolve({ width: image.width, height: image.height });
           image.onerror = () => resolve();
       });
   }
   ```
2. Get the rendered width (`clientWidth`) of the image element in the DOM.
3. Calculate the scale ratio `r = client_width / natural_width`.
4. Apply the scaled coordinates as absolute positioning styles to the red box overlay:
   ```javascript
   let pos = datailInfo.value.position; // [left, top, width, height]
   let r = detailimage.value.clientWidth / ImageWithHeight.value.width;
   
   boundingBoxStyle.value = {
       left: `${pos[0] * r}px`,
       top: `${pos[1] * r}px`,
       width: `${pos[2] * r}px`,
       height: `${pos[3] * r}px`
   };
   ```

The HTML overlay structure is:
```html
<div class="relative-position overflow-hidden">
    <!-- Image container -->
    <div class="dialog-detail-image">
        <q-img :src="item.scenesrc" :ratio="16/9"></q-img>
    </div>
    <!-- Red Bounding Box -->
    <div class="absolute dialog-detail-rect" :style="boundingBoxStyle"></div>
</div>
```
```scss
.dialog-detail-rect {
    position: absolute;
    border: 3px solid #f44336; /* red */
    pointer-events: none; /* Let pointer events pass through to image for zoom lens */
}
```

### B. Zoom Lens (Magnifying Glass)
When the magnifying glass option is enabled, moving the mouse over the image container shows a circular magnifying lens pointing to the corresponding region in the high-res original image.

```javascript
function zoomAreaMove(e) {
    if (!openMagnify.value) return;

    let subwidth = ImageWithHeight.value.width;
    let subheight = ImageWithHeight.value.height;
    let magnify_position = detailimage.value.getBoundingClientRect();
    
    // Mouse position relative to image container
    let mx = e.pageX - magnify_position.left;
    let my = e.pageY - magnify_position.top;

    if (mx < magnify_position.width && my < magnify_position.height && mx > 0 && my > 0) {
        showMagnify.value = true;
    } else {
        showMagnify.value = false;
    }

    if (showMagnify.value) {
        // Calculate background position offset for high-res original image
        let rx = Math.round((mx / detailimage.value.clientWidth) * subwidth - large.value.clientWidth / 2) * -1;
        let ry = Math.round((my / detailimage.value.clientHeight) * subheight - large.value.clientHeight / 2) * -1;

        let bgp = rx + "px " + ry + "px";
        
        // Position the circular lens element
        let px = mx - large.value.clientWidth / 2;
        let py = my - large.value.clientHeight / 2;

        large.value.style.left = px + "px";
        large.value.style.top = py + "px";
        large.value.style.backgroundPosition = bgp;
    }
}
```

The HTML for the magnifying lens:
```html
<div v-show="showMagnify" class="large absolute" ref="large" :style="`background: url('${item.scenesrc}') no-repeat`"></div>
```
The lens CSS:
```scss
.large {
    overflow: hidden;
    position: absolute;
    z-index: 2;
    width: 175px;
    height: 175px;
    border-radius: 100%;
    box-shadow: 0 0 0 7px rgba(255, 255, 255, 0.85), 0 0 7px 7px rgba(0, 0, 0, 0.25);
    pointer-events: none;
}
```
