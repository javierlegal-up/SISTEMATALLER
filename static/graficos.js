// Gráficos del reporte trimestral. Dibujados en SVG, sin librerías externas.
// Interacción: tooltip al pasar el mouse, leyenda que oculta/muestra series,
// animación al aparecer y redibujo al cambiar el tamaño de la ventana.
window.Graficos = (() => {
  const SVG = "http://www.w3.org/2000/svg";
  const numero = new Intl.NumberFormat("es-AR");
  const dinero = new Intl.NumberFormat("es-AR", { style: "currency", currency: "ARS", maximumFractionDigits: 0 });
  const compacto = new Intl.NumberFormat("es-AR", { notation: "compact", maximumFractionDigits: 1 });

  const esc = (texto) => String(texto).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

  const formatear =(valor, esDinero, corto) =>
    esDinero ? (corto ? "$" + compacto.format(valor / 100) : dinero.format(valor / 100)) : numero.format(valor);

  function nodo(tipo, atributos = {}, padre = null) {
    const elemento = document.createElementNS(SVG, tipo);
    for (const [clave, valor] of Object.entries(atributos)) elemento.setAttribute(clave, valor);
    if (padre) padre.appendChild(elemento);
    return elemento;
  }

  // Tope "redondo" del eje: 1, 2, 2,5 o 5 por potencia de 10.
  function escala(maximo, marcas = 3) {
    if (maximo <= 0) return { paso: 1, tope: marcas };
    const bruto = maximo / marcas;
    const potencia = 10 ** Math.floor(Math.log10(bruto));
    const paso = [1, 2, 2.5, 5, 10].map((f) => f * potencia).find((p) => p >= bruto && (p >= 1 ? Number.isInteger(p) : true)) || 10 * potencia;
    const pasoFinal = Math.max(paso, 1);
    return { paso: pasoFinal, tope: pasoFinal * Math.ceil(maximo / pasoFinal) };
  }

  function barras(contenedor, opciones) {
    const estado = {
      etiquetas: opciones.etiquetas,
      series: opciones.series,
      dinero: !!opciones.dinero,
      alto: opciones.alto || 240,
      animar: opciones.animar !== false,
      ocultas: new Set(),
      foco: null,
    };
    contenedor.classList.add("relative");
    const lienzo = document.createElement("div");
    const tooltip = document.createElement("div");
    tooltip.className = "pointer-events-none absolute z-10 hidden min-w-36 rounded-xl bg-slate-900/95 px-3 py-2 text-xs text-white shadow-xl backdrop-blur";
    const leyenda = document.createElement("div");
    leyenda.className = "mt-3 flex flex-wrap gap-1.5 print:gap-3";
    contenedor.replaceChildren(lienzo, tooltip, leyenda);

    function visibles() {
      return estado.series.filter((s) => !estado.ocultas.has(s.nombre));
    }

    // Leyenda: cada serie es una pastilla con su color; un clic la oculta o la muestra (con rebote).
    function dibujarLeyenda(rebote = null) {
      leyenda.replaceChildren();
      if (estado.series.length < 2 && !opciones.leyendaSiempre) return;
      for (const serie of estado.series) {
        const oculta = estado.ocultas.has(serie.nombre);
        const boton = document.createElement("button");
        boton.type = "button";
        boton.title = oculta ? "Mostrar" : "Ocultar";
        boton.className =
          "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold transition-all duration-300 " +
          "hover:-translate-y-0.5 hover:shadow-md active:scale-90 " +
          (oculta ? "border-dashed border-slate-300 bg-white text-slate-400 line-through" : "");
        if (!oculta) {
          boton.style.background = `${serie.color}14`;
          boton.style.borderColor = `${serie.color}55`;
          boton.style.color = serie.color === "#cbd5e1" ? "#475569" : serie.color;
          boton.addEventListener("mouseenter", () => { boton.style.background = `${serie.color}26`; });
          boton.addEventListener("mouseleave", () => { boton.style.background = `${serie.color}14`; });
        }
        boton.innerHTML = `<span class="h-2.5 w-2.5 rounded-full transition-transform duration-300" style="background:${oculta ? "#cbd5e1" : serie.color}; box-shadow:${oculta ? "none" : `0 0 0 3px ${serie.color}33`}"></span>`;
        boton.append(serie.nombre);
        boton.addEventListener("click", () => {
          if (oculta) estado.ocultas.delete(serie.nombre);
          else if (visibles().length > 1) estado.ocultas.add(serie.nombre);
          dibujar(true, serie.nombre);
        });
        if (rebote === serie.nombre) {
          boton.animate(
            [{ transform: "scale(.8)" }, { transform: "scale(1.12)" }, { transform: "scale(.96)" }, { transform: "scale(1)" }],
            { duration: 450, easing: "ease-out" },
          );
        }
        boton.addEventListener("mouseenter", () => { estado.foco = serie.nombre; resaltar(); });
        boton.addEventListener("mouseleave", () => { estado.foco = null; resaltar(); });
        leyenda.appendChild(boton);
      }
    }

    function resaltar() {
      lienzo.querySelectorAll("[data-serie]").forEach((barra) => {
        barra.style.opacity = estado.foco && barra.dataset.serie !== estado.foco ? "0.15" : "1";
      });
    }

    function dibujar(animar, rebote = null) {
      const ancho = Math.max(contenedor.clientWidth, 240);
      const alto = estado.alto;
      const series = visibles();
      const valores = series.flatMap((s) => s.valores);
      const maximo = Math.max(0, ...valores);
      const minimo = Math.min(0, ...valores);
      const { paso, tope } = escala(Math.max(maximo, -minimo));
      const piso = minimo < 0 ? -paso * Math.ceil(-minimo / paso) : 0;
      const izquierda = estado.dinero ? 52 : 28;
      const arriba = 14, abajo = 26;
      const areaAlto = alto - arriba - abajo;
      const y = (v) => arriba + (areaAlto * (tope - v)) / (tope - piso || 1);

      const svg = nodo("svg", { width: ancho, height: alto, viewBox: `0 0 ${ancho} ${alto}`, class: "block select-none overflow-visible" });

      // Líneas guía, muy suaves.
      for (let marca = piso; marca <= tope + paso / 1000; marca += paso) {
        nodo("line", { x1: izquierda, x2: ancho, y1: y(marca), y2: y(marca), stroke: marca === 0 ? "#e2e8f0" : "#f1f5f9" }, svg);
        const texto = nodo("text", { x: izquierda - 8, y: y(marca) + 4, "text-anchor": "end", "font-size": 11, fill: "#94a3b8" }, svg);
        texto.textContent = formatear(marca, estado.dinero, true);
      }

      const grupos = estado.etiquetas.length;
      const anchoGrupo = (ancho - izquierda) / grupos;
      const barra = Math.min(36, (anchoGrupo * 0.62) / Math.max(series.length, 1));
      const hueco = Math.min(6, barra * 0.2);
      const fondo = nodo("g", {}, svg);
      const capa = nodo("g", {}, svg);

      estado.etiquetas.forEach((etiqueta, i) => {
        const centro = izquierda + anchoGrupo * (i + 0.5);
        const inicio = centro - (series.length * barra) / 2;
        const texto = nodo("text", { x: centro, y: alto - 6, "text-anchor": "middle", "font-size": 12, "font-weight": 500, fill: "#64748b" }, svg);
        texto.textContent = etiqueta;

        // Zona sensible de todo el mes: resalta la columna y muestra el detalle.
        const zona = nodo("rect", { x: izquierda + anchoGrupo * i + 4, y: arriba - 6, width: anchoGrupo - 8, height: areaAlto + 12, rx: 12, fill: "#f8fafc", opacity: 0 }, fondo);
        const mostrar = (evento) => {
          zona.setAttribute("opacity", 1);
          const filas = series
            .map((s) => `<div class="flex items-center justify-between gap-4"><span class="flex items-center gap-1.5"><span class="h-2 w-2 rounded-full" style="background:${s.color}"></span>${esc(s.nombre)}</span><span class="font-semibold tabular-nums">${formatear(s.valores[i], estado.dinero)}</span></div>`)
            .join("");
          tooltip.innerHTML = `<div class="mb-1 font-semibold text-slate-300">${esc(opciones.titulos ? opciones.titulos[i] : etiqueta)}</div><div class="space-y-0.5">${filas}</div>`;
          tooltip.classList.remove("hidden");
          const caja = contenedor.getBoundingClientRect();
          let x = (evento.clientX ?? caja.left + centro) - caja.left + 14;
          if (x + tooltip.offsetWidth > caja.width) x -= tooltip.offsetWidth + 28;
          tooltip.style.left = `${Math.max(0, x)}px`;
          tooltip.style.top = `${Math.max(0, (evento.clientY ?? caja.top + arriba) - caja.top - 20)}px`;
        };
        const esconder = () => { zona.setAttribute("opacity", 0); tooltip.classList.add("hidden"); };
        const sensible = nodo("rect", { x: izquierda + anchoGrupo * i, y: 0, width: anchoGrupo, height: alto, fill: "transparent" }, svg);
        sensible.addEventListener("mousemove", mostrar);
        sensible.addEventListener("mouseleave", esconder);
        if (typeof opciones.alSeleccionar === "function") {
          const titulo = opciones.titulos ? opciones.titulos[i] : etiqueta;
          const seleccionar = () => opciones.alSeleccionar({
            indice: i,
            etiqueta,
            titulo,
            segmento: opciones.segmentos ? opciones.segmentos[i] : null,
            series: estado.series.map((serie) => ({ nombre: serie.nombre, valor: serie.valores[i] })),
          });
          sensible.setAttribute("role", "link");
          sensible.setAttribute("tabindex", "0");
          sensible.setAttribute("aria-label", `Abrir movimientos de ${titulo}`);
          sensible.style.cursor = "pointer";
          sensible.addEventListener("click", seleccionar);
          sensible.addEventListener("keydown", (evento) => {
            if (evento.key === "Enter" || evento.key === " ") {
              evento.preventDefault();
              seleccionar();
            }
          });
        }

        series.forEach((serie, j) => {
          const valor = serie.valores[i];
          const x = inicio + j * barra + hueco / 2;
          const alturaBarra = Math.abs(y(valor) - y(0));
          const rect = nodo("rect", {
            x, y: Math.min(y(valor), y(0)), width: Math.max(barra - hueco, 2), height: valor ? Math.max(alturaBarra, 2) : 0,
            rx: Math.min(6, (barra - hueco) / 2), fill: serie.color, "data-serie": serie.nombre,
          }, capa);
          rect.style.transition = "opacity .2s";
          if (animar) {
            rect.style.transformBox = "fill-box";
            rect.style.transformOrigin = valor >= 0 ? "bottom" : "top";
            rect.animate([{ transform: "scaleY(0)" }, { transform: "scaleY(1)" }], {
              duration: 650, delay: 60 * i + 25 * j, easing: "cubic-bezier(.2,.8,.2,1)", fill: "backwards",
            });
          }
        });
      });

      // Sin datos: un mensaje tranquilo en lugar de un gráfico vacío.
      if (!valores.some((v) => v !== 0)) {
        const vacio = nodo("text", { x: izquierda + (ancho - izquierda) / 2, y: arriba + areaAlto / 2, "text-anchor": "middle", "font-size": 13, fill: "#94a3b8" }, svg);
        vacio.textContent = "Sin movimiento en este período";
      }
      lienzo.replaceChildren(svg);
      dibujarLeyenda(rebote);
      resaltar();
    }

    let ultimoAncho = 0;
    new ResizeObserver(() => {
      if (Math.abs(contenedor.clientWidth - ultimoAncho) < 2) return;
      ultimoAncho = contenedor.clientWidth;
      dibujar(false);
    }).observe(contenedor);
    ultimoAncho = contenedor.clientWidth;
    dibujar(estado.animar);

    return {
      actualizar(series, nuevas = {}) {
        estado.series = series;
        Object.assign(estado, nuevas);
        estado.ocultas = new Set([...estado.ocultas].filter((n) => series.some((s) => s.nombre === n)));
        dibujar(estado.animar);
      },
      enfocar(nombre) { estado.foco = nombre; resaltar(); },
    };
  }

  // Línea mínima para las tarjetas de cifras.
  function mini(contenedor, valores, color) {
    const ancho = 96, alto = 32;
    const maximo = Math.max(...valores), minimo = Math.min(...valores);
    const rango = maximo - minimo || 1;
    const puntos = valores.map((v, i) => [
      (ancho - 6) * (valores.length > 1 ? i / (valores.length - 1) : 0.5) + 3,
      alto - 4 - ((alto - 8) * (v - minimo)) / rango,
    ]);
    const svg = nodo("svg", { width: ancho, height: alto, viewBox: `0 0 ${ancho} ${alto}` });
    const linea = nodo("polyline", { points: puntos.map((p) => p.join(",")).join(" "), fill: "none", stroke: color, "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round" }, svg);
    const largo = 200;
    linea.style.strokeDasharray = largo;
    linea.animate([{ strokeDashoffset: largo }, { strokeDashoffset: 0 }], { duration: 900, easing: "ease-out" });
    const [ux, uy] = puntos[puntos.length - 1];
    nodo("circle", { cx: ux, cy: uy, r: 3, fill: color }, svg);
    contenedor.replaceChildren(svg);
  }

  return { barras, mini, formatear };
})();
