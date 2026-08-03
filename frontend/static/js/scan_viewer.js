/* =============================================================
   3D 스캔 뷰어 — Three.js 로 재구성된 메시를 렌더한다.
   5뷰 visual-hull 재구성 결과(vertices/faces/vertex_colors)를 받아
   마우스로 돌려가며 형상을 검사한다. 불량 부위는 정점 색상(선셋 오렌지)
   으로 표시되므로 별도 마커 없이 표면에서 바로 보인다.

   OrbitControls 는 r128 CDN 에 별도 파일이라, 의존성을 줄이려 최소한의
   궤도 회전(드래그=회전, 휠=줌)을 직접 구현했다.
   ============================================================= */

const ScanViewer = {
  _color(varName, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
    return v || fallback;
  },

  create(container) {
    const THREE = window.THREE;
    const width = container.clientWidth;
    const height = container.clientHeight;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(this._color('--canvas-soft', '#1a1c20'));

    const camera = new THREE.PerspectiveCamera(42, width / height, 0.1, 100);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setSize(width, height);
    container.innerHTML = '';
    container.appendChild(renderer.domElement);

    // 조명 — 형상의 굴곡이 드러나도록 앰비언트 + 방향광 2개.
    // 정점 색상을 살리기 위해 앰비언트를 넉넉히 준다.
    scene.add(new THREE.AmbientLight(0xffffff, 0.75));
    const key = new THREE.DirectionalLight(0xffffff, 0.55);
    key.position.set(3, 4, 5);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0xffffff, 0.25);
    fill.position.set(-3, -2, -4);
    scene.add(fill);

    // 메시를 담을 그룹(교체 시 그룹만 비우면 된다)
    const meshGroup = new THREE.Group();
    scene.add(meshGroup);

    const state = {
      THREE, scene, camera, renderer, meshGroup,
      rotX: 0.35, rotY: -0.6, distance: 3.4, raf: null, disposed: false,
      autoRotate: false,
    };

    this._bindControls(container, state);
    camera.position.set(0, 0, state.distance);

    const animate = () => {
      if (state.disposed) return;
      state.raf = requestAnimationFrame(animate);
      if (state.autoRotate) state.rotY += 0.004;
      meshGroup.rotation.x = state.rotX;
      meshGroup.rotation.y = state.rotY;
      camera.position.z = state.distance;
      renderer.render(scene, camera);
    };
    animate();

    const ro = new ResizeObserver(() => {
      const w = container.clientWidth, h = container.clientHeight;
      if (!w || !h) return;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
    });
    ro.observe(container);
    state.resizeObserver = ro;

    return {
      /** mesh: { vertices:[[x,y,z]], faces:[[a,b,c]], vertex_colors:[[r,g,b]] } */
      loadMesh: (mesh) => this._loadMesh(state, mesh),
      setAutoRotate: (on) => { state.autoRotate = on; },
      reset: () => { state.rotX = 0.35; state.rotY = -0.6; state.distance = 3.4; },
      dispose: () => {
        state.disposed = true;
        cancelAnimationFrame(state.raf);
        ro.disconnect();
        this._clearGroup(state);
        renderer.dispose();
      },
    };
  },

  _clearGroup(state) {
    const g = state.meshGroup;
    while (g.children.length) {
      const child = g.children.pop();
      if (child.geometry) child.geometry.dispose();
      if (child.material) child.material.dispose();
    }
  },

  _loadMesh(state, mesh) {
    const { THREE } = state;
    this._clearGroup(state);

    if (!mesh || !mesh.vertices || mesh.vertices.length === 0) {
      return { vertexCount: 0, faceCount: 0 };
    }

    const geometry = new THREE.BufferGeometry();

    // 정점/색상을 평탄 Float32Array 로 변환
    const positions = new Float32Array(mesh.vertices.length * 3);
    const colors = new Float32Array(mesh.vertices.length * 3);
    for (let i = 0; i < mesh.vertices.length; i++) {
      positions[i * 3] = mesh.vertices[i][0];
      positions[i * 3 + 1] = mesh.vertices[i][1];
      positions[i * 3 + 2] = mesh.vertices[i][2];
      const c = mesh.vertex_colors[i] || [0.72, 0.73, 0.75];
      colors[i * 3] = c[0];
      colors[i * 3 + 1] = c[1];
      colors[i * 3 + 2] = c[2];
    }
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));

    // 면 인덱스
    const indices = new Uint32Array(mesh.faces.length * 3);
    for (let i = 0; i < mesh.faces.length; i++) {
      indices[i * 3] = mesh.faces[i][0];
      indices[i * 3 + 1] = mesh.faces[i][1];
      indices[i * 3 + 2] = mesh.faces[i][2];
    }
    geometry.setIndex(new THREE.BufferAttribute(indices, 1));
    geometry.computeVertexNormals();

    const material = new THREE.MeshStandardMaterial({
      vertexColors: true,
      roughness: 0.75,
      metalness: 0.05,
      flatShading: false,
    });

    const surface = new THREE.Mesh(geometry, material);
    state.meshGroup.add(surface);

    // 형상 윤곽을 살짝 강조하는 와이어프레임 오버레이(헤어라인 톤, 아주 옅게)
    const wire = new THREE.LineSegments(
      new THREE.WireframeGeometry(geometry),
      new THREE.LineBasicMaterial({
        color: new THREE.Color(this._color('--hairline', '#212327')),
        transparent: true,
        opacity: 0.15,
      })
    );
    state.meshGroup.add(wire);

    return { vertexCount: mesh.vertices.length, faceCount: mesh.faces.length };
  },

  _bindControls(container, state) {
    let dragging = false, lastX = 0, lastY = 0;

    const onDown = (e) => {
      dragging = true;
      const p = e.touches ? e.touches[0] : e;
      lastX = p.clientX; lastY = p.clientY;
    };
    const onMove = (e) => {
      if (!dragging) return;
      const p = e.touches ? e.touches[0] : e;
      state.rotY += (p.clientX - lastX) * 0.008;
      state.rotX += (p.clientY - lastY) * 0.008;
      state.rotX = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, state.rotX));
      lastX = p.clientX; lastY = p.clientY;
    };
    const onUp = () => { dragging = false; };
    const onWheel = (e) => {
      e.preventDefault();
      // 확대 범위: 형상에 바짝 붙어 표면 결함을 보거나 멀리서 전체를 본다.
      state.distance = Math.max(1.6, Math.min(7, state.distance + Math.sign(e.deltaY) * 0.3));
    };

    container.addEventListener('mousedown', onDown);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    container.addEventListener('touchstart', onDown, { passive: true });
    container.addEventListener('touchmove', onMove, { passive: true });
    container.addEventListener('touchend', onUp);
    container.addEventListener('wheel', onWheel, { passive: false });
  },
};
