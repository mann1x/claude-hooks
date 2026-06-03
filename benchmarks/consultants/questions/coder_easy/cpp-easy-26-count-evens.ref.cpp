#include <iostream>
int main() {
    long long x;
    int n = 0;
    while (std::cin >> x)
        if (x % 2 == 0) n++;
    std::cout << n << std::endl;
    return 0;
}
