#include <iostream>
int main() {
    long long x, m;
    if (!(std::cin >> m)) return 0;
    while (std::cin >> x)
        if (x < m) m = x;
    std::cout << m << std::endl;
    return 0;
}
