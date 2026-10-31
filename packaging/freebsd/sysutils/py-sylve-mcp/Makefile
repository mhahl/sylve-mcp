PORTNAME=	sylve-mcp
DISTVERSION=	0.1.0
CATEGORIES=	sysutils python
MASTER_SITES=	PYPI
PKGNAMEPREFIX=	${PYTHON_PKGNAMEPREFIX}

MAINTAINER=	ports@FreeBSD.org
COMMENT=	Model Context Protocol server for the Sylve FreeBSD hypervisor
WWW=		https://github.com/mhahl/sylve-mcp

LICENSE=	BSD2CLAUSE
LICENSE_FILE=	${WRKSRC}/LICENSE

BUILD_DEPENDS=	${PYTHON_PKGNAMEPREFIX}setuptools>=69:devel/py-setuptools@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}wheel>=0:devel/py-wheel@${PY_FLAVOR}
RUN_DEPENDS=	${PYTHON_PKGNAMEPREFIX}httpx>=0.25.0:www/py-httpx@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}pyyaml>=6.0:devel/py-pyyaml@${PY_FLAVOR}

USES=		python
USE_PYTHON=	pep517 autoplist concurrent

NO_ARCH=	yes

.include <bsd.port.mk>